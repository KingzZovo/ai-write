"""Secondary-cast roster (F4 / ainovel).

Adapted from voocel/ainovel-cli's cast tracking (design idea; wording our own).

Named secondary characters that vanish for 80 chapters and reappear tend to be
rewritten as a different person. This module keeps a lightweight per-character
first_seen / last_seen / appearance_count ledger (separate table, pure-PG, zero
LLM) so generation can be reminded to re-read a character's last appearance
before writing them again.

Pure functions here are side-effect free; the Celery wiring lives in the
incremental recompute task (app.tasks.style_tasks), which counts appearances
per chapter (persisted in chapter_style_stats.appearances_json) and rebuilds
the roster from those rows via ``rebuild_roster`` (idempotent absolute-value
upsert, not increments).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_RECALL_GAP = 10  # chapters: re-read reminder kicks in past this gap


def count_appearances(
    text: str,
    names: set[str],
    alias_map: dict[str, list[str]] | None = None,
) -> dict[str, int]:
    """Count non-overlapping appearances of each name (len>=2) in ``text``.

    When names are substrings of each other (e.g. "林惊蛰" / "林惊"), longer
    names are matched first and their spans removed, so the shorter name is not
    double-counted inside the longer one.

    ``alias_map`` maps a canonical name to its exact-string aliases
    (e.g. {"萧炎": ["炎帝"]}). Alias occurrences are counted into the
    canonical name's tally so 出场统计/last_seen reflect the real character.
    An alias that collides with another tracked name is ignored (the name
    keeps its own tally); alias keys not present in ``names`` are ignored.
    """
    if not text or not names:
        return {}
    # token -> canonical name it counts toward.
    token_owner: dict[str, str] = {
        n: n for n in names if n and len(n) >= 2
    }
    if alias_map:
        for canonical in sorted(alias_map):
            if canonical not in token_owner:
                continue
            for alias in alias_map[canonical] or []:
                if not isinstance(alias, str):
                    continue
                alias = alias.strip()
                if len(alias) < 2 or alias in token_owner:
                    continue
                token_owner[alias] = canonical
    # Longest tokens first so substrings don't steal their matches.
    ordered = sorted(token_owner, key=len, reverse=True)
    remaining = text
    counts: dict[str, int] = {}
    for token in ordered:
        c = remaining.count(token)
        if c:
            canonical = token_owner[token]
            counts[canonical] = counts.get(canonical, 0) + c
            # Blank out matched spans so a shorter contained name won't recount.
            remaining = remaining.replace(token, "\x00" * len(token))
    return counts


async def load_alias_map(db, project_id) -> dict[str, list[str]]:
    """Canonical name -> exact-string aliases, from characters.profile_json.

    Aliases are written by the settings extractor (characters_extraction
    prompt) into ``profile_json["aliases"]``. Defensive: missing/odd shapes
    yield no entry.
    """
    from sqlalchemy import select

    from app.models.project import Character

    rows = (
        await db.execute(
            select(Character.name, Character.profile_json).where(
                Character.project_id == project_id
            )
        )
    ).all()
    out: dict[str, list[str]] = {}
    for name, profile in rows:
        if not name or not isinstance(profile, dict):
            continue
        aliases = profile.get("aliases")
        if not isinstance(aliases, list):
            continue
        clean = [a.strip() for a in aliases if isinstance(a, str) and a.strip()]
        if clean:
            out[name] = clean
    return out


def aggregate_appearances(
    per_chapter: list[tuple[int, dict[str, int]]],
) -> dict[str, dict[str, int]]:
    """Fold per-chapter appearance counts into per-character roster totals.

    ``per_chapter`` is ``[(global_idx, {name: count_in_that_chapter}), ...]``
    (one entry per chapter, alias folding already applied at count time by
    ``count_appearances``). Returns ``{name: {"first_seen", "last_seen",
    "count"}}``. Pure and derivable: running it again over the same rows
    yields the same totals, which is what makes the roster rebuild idempotent.
    """
    totals: dict[str, dict[str, int]] = {}
    for idx, counts in per_chapter:
        for name, c in (counts or {}).items():
            c = int(c or 0)
            if not name or c <= 0:
                continue
            cur = totals.get(name)
            if cur is None:
                totals[name] = {"first_seen": idx, "last_seen": idx, "count": c}
            else:
                cur["first_seen"] = min(cur["first_seen"], idx)
                cur["last_seen"] = max(cur["last_seen"], idx)
                cur["count"] += c
    return totals


async def rebuild_roster(
    db, project_id, per_chapter: list[tuple[int, dict[str, int]]],
    valid_names: set[str] | None = None,
) -> None:
    """Rebuild the character_appearances roster from per-chapter counts.

    Replaces the old per-chapter ``appearance_count = appearance_count + c``
    increment (non-idempotent: repeated recomputes inflated counts). Totals
    are aggregated from the per-chapter rows and written as ABSOLUTE values;
    roster rows for names no longer appearing (or no longer in
    ``valid_names``, e.g. a deleted character) are removed. Running this any
    number of times over the same rows converges to the same roster.
    """
    from sqlalchemy import delete
    from sqlalchemy.dialects.postgresql import insert

    from app.models.project import CharacterAppearance

    totals = aggregate_appearances(per_chapter)
    if valid_names is not None:
        totals = {n: agg for n, agg in totals.items() if n in valid_names}
    for name, agg in totals.items():
        stmt = (
            insert(CharacterAppearance)
            .values(
                project_id=str(project_id),
                character_name=name,
                first_seen_chapter=agg["first_seen"],
                last_seen_chapter=agg["last_seen"],
                appearance_count=agg["count"],
            )
            .on_conflict_do_update(
                index_elements=["project_id", "character_name"],
                set_={
                    "first_seen_chapter": agg["first_seen"],
                    "last_seen_chapter": agg["last_seen"],
                    "appearance_count": agg["count"],
                },
            )
        )
        await db.execute(stmt)
    stale = delete(CharacterAppearance).where(
        CharacterAppearance.project_id == str(project_id)
    )
    if totals:
        stale = stale.where(
            CharacterAppearance.character_name.not_in(list(totals))
        )
    await db.execute(stale)


def render_roster_block(
    rows: list, current_idx: int, *, max_entries: int = 15, max_chars: int = 600
) -> str:
    """Render the secondary-cast roster, most-recently-seen first.

    ``rows`` is an iterable of objects/dicts with ``character_name`` and
    ``last_seen_chapter``. Characters last seen more than ``_RECALL_GAP``
    chapters ago get an explicit "re-read chapter N" reminder.
    """
    def _get(r, k):
        return r.get(k) if isinstance(r, dict) else getattr(r, k, None)

    items = [
        (_get(r, "character_name"), _get(r, "last_seen_chapter"))
        for r in rows
    ]
    items = [(n, ls) for n, ls in items if n and ls is not None]
    if not items:
        return ""
    items.sort(key=lambda nl: -nl[1])
    items = items[:max_entries]

    lines = ["【配角名册（最近出场倒序）】写到下列角色时对齐其既有口吻/外貌/状态："]
    for name, last_seen in items:
        gap = current_idx - last_seen
        if gap > _RECALL_GAP:
            lines.append(f"- {name}：上次出场 [CH-{last_seen}]（已隔{gap}章），写到时回读对齐。")
        else:
            lines.append(f"- {name}：上次出场 [CH-{last_seen}]。")

    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
    # If only the header fits, suppress (no signal).
    if len(out) <= 1:
        return ""
    return "\n".join(out)


# Han-token extractor for matching outline text against foreshadow descriptions.
_HAN_RUN_RE = re.compile(r"[一-鿿]{2,}")


def outline_tokens(text: str, *, min_len: int = 4) -> set[str]:
    """Coarse Han substrings (len>=min_len) from outline text for fuzzy matching."""
    tokens: set[str] = set()
    for run in _HAN_RUN_RE.findall(text or ""):
        if len(run) >= min_len:
            tokens.add(run)
    return tokens
