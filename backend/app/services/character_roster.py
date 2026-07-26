"""Secondary-cast roster (F4 / ainovel).

Adapted from voocel/ainovel-cli's cast tracking (design idea; wording our own).

Named secondary characters that vanish for 80 chapters and reappear tend to be
rewritten as a different person. This module keeps a lightweight per-character
first_seen / last_seen / appearance_count ledger (separate table, pure-PG, zero
LLM) so generation can be reminded to re-read a character's last appearance
before writing them again.

Pure functions here are side-effect free; the DB upsert and the Celery wiring
live in the recompute task (app.tasks.style_tasks) which already pulls every
chapter's text once.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_RECALL_GAP = 10  # chapters: re-read reminder kicks in past this gap


def count_appearances(text: str, names: set[str]) -> dict[str, int]:
    """Count non-overlapping appearances of each name (len>=2) in ``text``.

    When names are substrings of each other (e.g. "林惊蛰" / "林惊"), longer
    names are matched first and their spans removed, so the shorter name is not
    double-counted inside the longer one.
    """
    if not text or not names:
        return {}
    # Longest names first so substrings don't steal their matches.
    ordered = sorted((n for n in names if n and len(n) >= 2), key=len, reverse=True)
    remaining = text
    counts: dict[str, int] = {}
    for name in ordered:
        c = remaining.count(name)
        if c:
            counts[name] = c
            # Blank out matched spans so a shorter contained name won't recount.
            remaining = remaining.replace(name, "\x00" * len(name))
    return counts


async def update_roster_for_chapter(
    db, project_id, global_idx: int, text: str, names: set[str]
) -> None:
    """Upsert first_seen/last_seen/appearance_count for names seen in a chapter."""
    from sqlalchemy.dialects.postgresql import insert

    from app.models.project import CharacterAppearance

    counts = count_appearances(text, names)
    for name, c in counts.items():
        stmt = (
            insert(CharacterAppearance)
            .values(
                project_id=str(project_id),
                character_name=name,
                first_seen_chapter=global_idx,
                last_seen_chapter=global_idx,
                appearance_count=c,
            )
            .on_conflict_do_update(
                index_elements=["project_id", "character_name"],
                set_={
                    "first_seen_chapter": _least(global_idx),
                    "last_seen_chapter": _greatest(global_idx),
                    "appearance_count": CharacterAppearance.appearance_count + c,
                },
            )
        )
        await db.execute(stmt)


def _least(idx: int):
    from sqlalchemy import func

    from app.models.project import CharacterAppearance

    return func.least(CharacterAppearance.first_seen_chapter, idx)


def _greatest(idx: int):
    from sqlalchemy import func

    from app.models.project import CharacterAppearance

    return func.greatest(CharacterAppearance.last_seen_chapter, idx)


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
