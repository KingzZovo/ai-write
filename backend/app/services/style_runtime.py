"""StyleRuntime: Resolves active style rules for a given generation context.

Priority resolution order:
  1. Project settings binding (settings_json.style_profile_id /
     style_reference.profile_id / default_style_profile_id) — loaded directly
     by id regardless of the profile's bind_level. Live profiles bind to
     reference-book ids, so bind-level matching alone never resolved them.
  2. Chapter-level binding
  3. Book/project-level binding (bind_target_id == project_id)
  4. Global active profiles (lowest priority)

Compiles the resolved profile(s) into prompt instructions, preferring a
consolidated dossier style block (ReferenceBook.metadata_json['dossier'])
when the profile is bound to a reference book that has one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import StyleProfile
from app.services.style_compiler import compile_style, compile_anti_ai_instructions

logger = logging.getLogger(__name__)

# Size cap for the style block injected into writer prompts ([风格要求]).
STYLE_INJECTION_MAX_CHARS = 1200


@dataclass
class ResolvedStyle:
    """Result of the full style resolution chain for one generation context."""

    profile: StyleProfile | None = None
    reference_book_id: str | None = None
    style_text: str = ""
    source: str = ""  # "dossier" | "author_dossier" | "compiled" | ""


# ---------------------------------------------------------------------------
# Dossier contract (forward-compatible; built by the consolidation pipeline)
# ---------------------------------------------------------------------------
# ReferenceBook.metadata_json['dossier'] = {
#   style_block: str(<=1200), structure_block: str(<=800),
#   world_block: str(<=1000), style_data: {}, plot_data: {}, world_data: {},
#   consolidated_at, source_counts,
# }


def get_dossier_block(book, key: str) -> str:
    """Return dossier[key] from a ReferenceBook or AuthorDossier, else "".

    Same accessor for both dossier granularities: ReferenceBook rows carry
    ``metadata_json['dossier']``; AuthorDossier rows carry ``dossier_json``
    directly (same contract shape, larger caps).
    """
    dossier = getattr(book, "dossier_json", None)  # AuthorDossier rows
    if not isinstance(dossier, dict) or not dossier:
        meta = getattr(book, "metadata_json", None) or {}
        if not isinstance(meta, dict):
            return ""
        dossier = meta.get("dossier")
    if not isinstance(dossier, dict):
        return ""
    block = dossier.get(key)
    return block.strip() if isinstance(block, str) and block.strip() else ""


def build_style_injection_block(style_text: str, max_chars: int = STYLE_INJECTION_MAX_CHARS) -> str:
    """Build the writer-prompt style block, matching the async path's format
    (generation_tasks appends "\\n\\n[风格要求] " + style_text)."""
    text = (style_text or "").strip()
    if not text:
        return ""
    return "[风格要求] " + text[:max_chars]


def _settings_author_name(settings_json: dict) -> str | None:
    """Author binding from project settings (style_reference.author_name)."""
    if not isinstance(settings_json, dict):
        return None
    style_ref = settings_json.get("style_reference") or {}
    if isinstance(style_ref, dict):
        name = style_ref.get("author_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


async def load_author_dossier(db: AsyncSession, author: str):
    """Load the AuthorDossier row for an author, or None.

    Tolerates a missing table (migration not applied yet): logs and rolls
    the failed transaction back so the caller's session stays usable.
    """
    if not author:
        return None
    from app.models.author_dossier import AuthorDossier

    try:
        result = await db.execute(
            select(AuthorDossier).where(AuthorDossier.author == author).limit(1)
        )
        return result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning("author dossier lookup failed for %s: %s", author, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _settings_style_profile_id(settings_json: dict) -> str | None:
    if not isinstance(settings_json, dict):
        return None
    sid = settings_json.get("style_profile_id")
    if not sid:
        style_ref = settings_json.get("style_reference") or {}
        if isinstance(style_ref, dict):
            sid = style_ref.get("profile_id")
    if not sid:
        sid = settings_json.get("default_style_profile_id")
    return sid if isinstance(sid, str) and sid.strip() else None


async def _load_project_settings(db: AsyncSession, project_id: str | UUID) -> dict:
    from app.models.project import Project

    try:
        project = await db.get(Project, str(project_id))
    except Exception:
        return {}
    if project is None:
        return {}
    settings_json = project.settings_json or {}
    return settings_json if isinstance(settings_json, dict) else {}


async def resolve_style_prompt(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
) -> str:
    """Resolve and compile the active style text for a generation context.

    Returns a prompt string ready for injection (dossier style_block when the
    resolved profile is book-bound and the book has one, else compiled rules).
    """
    ctx = await resolve_style_context(db, project_id, chapter_id)
    return ctx.style_text


async def resolve_anti_ai_prompt(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
) -> str:
    """Resolve only the Anti-AI instructions for a generation context."""
    profile = await resolve_active_profile(db, project_id, chapter_id)
    if profile is None:
        return ""
    return compile_anti_ai_instructions(profile)


async def resolve_active_profile(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
    settings_json: dict | None = None,
) -> StyleProfile | None:
    """Find the highest-priority profile for the given context.

    Resolution order:
    1. Project settings profile id (style_profile_id / style_reference.profile_id
       / default_style_profile_id) -> load directly by id, ANY bind_level.
    2. Chapter-bound profile (bind_level='chapter', bind_target_id=chapter_id)
    3. Project-bound profile (bind_level='book', bind_target_id=project_id)
    4. Global active profile (bind_level='global')
    """
    # 1. Settings-declared profile (direct id load; bind_level irrelevant)
    if settings_json is None:
        settings_json = await _load_project_settings(db, project_id)
    settings_profile_id = _settings_style_profile_id(settings_json)
    if settings_profile_id:
        try:
            profile = await db.get(StyleProfile, settings_profile_id)
        except Exception:
            profile = None
        if profile is not None:
            return profile
        logger.warning(
            "Settings style_profile_id=%s not found; falling back to bind-level chain",
            settings_profile_id,
        )

    # 2. Chapter-level
    if chapter_id:
        result = await db.execute(
            select(StyleProfile).where(
                StyleProfile.is_active == 1,
                StyleProfile.bind_level == "chapter",
                StyleProfile.bind_target_id == str(chapter_id),
            ).limit(1)
        )
        profile = result.scalar_one_or_none()
        if profile:
            return profile

    # 3. Project-level
    result = await db.execute(
        select(StyleProfile).where(
            StyleProfile.is_active == 1,
            StyleProfile.bind_level == "book",
            StyleProfile.bind_target_id == str(project_id),
        ).limit(1)
    )
    profile = result.scalar_one_or_none()
    if profile:
        return profile

    # 4. Global
    result = await db.execute(
        select(StyleProfile).where(
            StyleProfile.is_active == 1,
            StyleProfile.bind_level == "global",
        ).order_by(StyleProfile.updated_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def derive_reference_book_id(
    db: AsyncSession, profile: StyleProfile | None
) -> str | None:
    """Derive the reference book a profile was extracted from.

    Order: explicit source_book_id FK, then bind_level=='book' bind_target_id
    (validated against reference_books — legacy project-bound profiles reuse
    the same bind_level with a project id, which must not be treated as a
    book id).
    """
    if profile is None:
        return None
    from app.models.project import ReferenceBook

    source_book_id = getattr(profile, "source_book_id", None)
    if source_book_id:
        return str(source_book_id)
    if getattr(profile, "bind_level", "") == "book" and getattr(profile, "bind_target_id", None):
        candidate = str(profile.bind_target_id)
        try:
            book = await db.get(ReferenceBook, candidate)
        except Exception:
            book = None
        if book is not None:
            return candidate
    return None


async def resolve_reference_book_id(
    db: AsyncSession,
    project_id: str | UUID,
    settings_json: dict | None = None,
    profile: StyleProfile | None = None,
) -> str | None:
    """Resolve the reference book bound to this project for recall filtering.

    Chain: settings style_reference.reference_book_id / .book_id /
    settings.reference_book_id, then the resolved style profile's book binding
    (defect-2 derivation). Returns None when nothing resolves — callers should
    then SKIP reference recall rather than search unfiltered across all books.
    """
    if settings_json is None:
        settings_json = await _load_project_settings(db, project_id)
    style_ref = settings_json.get("style_reference") or {}
    if isinstance(style_ref, dict):
        for key in ("reference_book_id", "book_id"):
            rid = style_ref.get(key)
            if isinstance(rid, str) and rid.strip():
                return rid
    rid = settings_json.get("reference_book_id")
    if isinstance(rid, str) and rid.strip():
        return rid

    if profile is None:
        profile = await resolve_active_profile(db, project_id, settings_json=settings_json)
    return await derive_reference_book_id(db, profile)


async def production_style_text_for_profile(
    db: AsyncSession, profile: StyleProfile, settings_json: dict | None = None
) -> tuple[str, str, str | None]:
    """Build the production writer-injection style text for a profile.

    Returns (style_text, source, reference_book_id). Preference order:
    book dossier style_block > author dossier style_block (when settings
    carry ``style_reference.author_name``) > compile_style WITHOUT
    sample_passages few-shot (PR-NO-RAW-INJECT).
    """
    from app.models.project import ReferenceBook

    reference_book_id = await derive_reference_book_id(db, profile)
    if reference_book_id:
        try:
            book = await db.get(ReferenceBook, reference_book_id)
        except Exception:
            book = None
        if book is not None:
            block = get_dossier_block(book, "style_block")
            if block:
                return block, "dossier", reference_book_id
    author = _settings_author_name(settings_json or {})
    if author:
        author_row = await load_author_dossier(db, author)
        if author_row is not None:
            block = get_dossier_block(author_row, "style_block")
            if block:
                return block, "author_dossier", reference_book_id
    return compile_style(profile, include_samples=False), "compiled", reference_book_id


async def resolve_author_structure_block(
    db: AsyncSession, settings_json: dict | None
) -> str:
    """Author-tier structure injection block for an author-only binding.

    When project settings carry ``style_reference.author_name`` but no
    structure book resolves, this returns the author dossier's
    structure_block through the same ``get_dossier_block`` accessor
    generate.py already uses for book dossiers (AuthorDossier rows are
    accepted by that helper directly).
    """
    author = _settings_author_name(settings_json or {})
    if not author:
        return ""
    author_row = await load_author_dossier(db, author)
    if author_row is None:
        return ""
    return get_dossier_block(author_row, "structure_block")


async def resolve_style_context(
    db: AsyncSession,
    project_id: str | UUID,
    chapter_id: str | UUID | None = None,
    style_id: str | UUID | None = None,
) -> ResolvedStyle:
    """Full resolution: profile (explicit id > settings > bind chain) plus the
    derived reference book id and the production style text (book dossier >
    author dossier > compiled)."""
    settings_json = await _load_project_settings(db, project_id)
    profile: StyleProfile | None = None
    if style_id:
        try:
            profile = await db.get(StyleProfile, str(style_id))
        except Exception:
            profile = None
    if profile is None:
        profile = await resolve_active_profile(
            db, project_id, chapter_id, settings_json=settings_json
        )
    if profile is None:
        # Author-only binding: no profile resolves, but a consolidated
        # author dossier can still drive the style injection.
        author = _settings_author_name(settings_json)
        if author:
            author_row = await load_author_dossier(db, author)
            if author_row is not None:
                block = get_dossier_block(author_row, "style_block")
                if block:
                    return ResolvedStyle(style_text=block, source="author_dossier")
        return ResolvedStyle()
    style_text, source, reference_book_id = await production_style_text_for_profile(
        db, profile, settings_json=settings_json
    )
    return ResolvedStyle(
        profile=profile,
        reference_book_id=reference_book_id,
        style_text=style_text,
        source=source,
    )


# ---------------------------------------------------------------------------
# Reference proper-noun scrub (structure prompts leaking source names)
# ---------------------------------------------------------------------------


def collect_reference_proper_nouns(book) -> list[str]:
    """Collect known proper nouns for a reference book from its metadata.

    Sources: book title, metadata_json character-name lists (top-level
    'characters'/'character_names' and dossier world_data/plot_data/style_data
    equivalents). Names shorter than 2 chars are ignored.
    """
    nouns: list[str] = []

    def _add(value) -> None:
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str):
            v = value.strip().strip("《》")
            if len(v) >= 2 and v not in nouns:
                nouns.append(v)

    _add(getattr(book, "title", None))
    meta = getattr(book, "metadata_json", None) or {}
    if not isinstance(meta, dict):
        return nouns

    def _add_from(container: dict) -> None:
        for key in ("characters", "character_names"):
            vals = container.get(key)
            if isinstance(vals, list):
                for v in vals:
                    _add(v)

    _add_from(meta)
    dossier = meta.get("dossier")
    if isinstance(dossier, dict):
        for data_key in ("world_data", "plot_data", "style_data"):
            data = dossier.get(data_key)
            if isinstance(data, dict):
                _add_from(data)
    return nouns


def scrub_reference_proper_nouns(text: str, book) -> str:
    """Remove a reference book's known proper nouns from a prompt block.

    Limitation: only names recorded in the book's metadata (title, character
    lists, dossier data) can be scrubbed. When metadata carries no names we
    log a warning and return the text unchanged — a blind regex pass that
    drops capitalized/rare-name tokens is too destructive for Chinese prose.
    """
    if not text:
        return text
    nouns = collect_reference_proper_nouns(book)
    if not nouns:
        logger.warning(
            "No known proper nouns in metadata for reference book %s; "
            "structure block injected unscrubbed",
            getattr(book, "id", "?"),
        )
        return text
    for noun in nouns:
        text = text.replace(noun, "")
    return text
