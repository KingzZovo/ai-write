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
    canonical name's tally as mapped mentions, not verified scene presence.
    An alias that collides with another tracked name is ignored (the name
    keeps its own tally); alias keys not present in ``names`` are ignored.
    Shared aliases are consumed without credit to any person: string matching
    cannot establish which owner was mentioned or advance their last_seen.
    """
    if not text or not names:
        return {}
    # token -> canonical name it counts toward.
    token_owner: dict[str, str | None] = {
        n: n for n in names if n and len(n) >= 2
    }
    canonical_tokens = set(token_owner)
    alias_owners: dict[str, set[str]] = {}
    if alias_map:
        for canonical in sorted(alias_map):
            if canonical not in canonical_tokens:
                continue
            for alias in alias_map[canonical] or []:
                if not isinstance(alias, str):
                    continue
                alias = alias.strip()
                if len(alias) < 2 or alias in canonical_tokens:
                    continue
                alias_owners.setdefault(alias, set()).add(canonical)
    for alias, owners in alias_owners.items():
        token_owner[alias] = next(iter(owners)) if len(owners) == 1 else None
    # Consume ambiguous aliases too, so embedded shorter names are not credited.
    ordered = sorted(token_owner, key=lambda token: (-len(token), token))
    remaining = text
    counts: dict[str, int] = {}
    for token in ordered:
        c = remaining.count(token)
        if c:
            canonical = token_owner[token]
            if canonical is not None:
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
    # 2026-11-17:空输入不得清空全书名册。原先 `totals` 为空时 DELETE 没有 not_in
    # 条件,等于把该项目的 character_appearances 整表删掉(上游统计被跳过/异常后
    # 仍调用 rebuild 就会发生),后续章节失去"上次提到 CH-N"的提示。
    if not totals:
        logger.warning(
            "character roster rebuild skipped: empty input would wipe project roster (%s)",
            project_id,
        )
        return
    stale = delete(CharacterAppearance).where(
        CharacterAppearance.project_id == str(project_id)
    ).where(CharacterAppearance.character_name.not_in(list(totals)))
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

    lines = ["【人物提及记录（最近提及倒序）】仅供回读定位；提及不等于出场或知情，口吻/外貌/状态须核对原文："]
    for name, last_seen in items:
        gap = current_idx - last_seen
        if gap > _RECALL_GAP:
            lines.append(f"- {name}：上次提及 [CH-{last_seen}]（已隔{gap}章），写到时回读对齐。")
        else:
            lines.append(f"- {name}：上次提及 [CH-{last_seen}]。")

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


async def load_historical_roster(db, project_id, *, before_global_idx: int) -> list[dict]:
    """Derive mention counts from current, completed, unambiguous earlier prose.

    Whole-book appearance caches cannot answer a historical query: a later
    appearance hides the earlier last mention, and edits/deletions can leave
    stale counts. Stream authoritative bodies once per ContextPack instead.
    Counts guide recall only; a name mention does not establish presence.
    """
    from sqlalchemy import func, select
    from app.models.project import Chapter, Character, Volume

    if type(before_global_idx) is not int or before_global_idx <= 0:
        return []
    profiles = (await db.execute(select(Character.name, Character.profile_json)
        .where(Character.project_id == str(project_id))
        .execution_options(autoflush=False))).all()
    names = {name for name, _ in profiles if name}
    if not names:
        return []
    aliases = {}
    for name, profile in profiles:
        if isinstance(profile, dict) and isinstance(profile.get('aliases'), list):
            aliases[name] = profile['aliases']
    # Include draft rows in duplicate detection; their index still makes the
    # project's chronology ambiguous. Apply completed status only afterwards.
    unique_indices = (select(Chapter.global_idx).join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == str(project_id), Chapter.global_idx > 0,
               Chapter.global_idx < before_global_idx)
        .group_by(Chapter.global_idx).having(func.count(Chapter.id) == 1))
    stmt = (select(Chapter.global_idx, Chapter.content_text)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == str(project_id), Chapter.status == 'completed',
               Chapter.global_idx.in_(unique_indices))
        .order_by(Chapter.global_idx).execution_options(autoflush=False, yield_per=32))
    totals = {}
    result = await db.stream(stmt)
    try:
        async for idx, body in result:
            for name, count in count_appearances(body or '', names, aliases).items():
                current = totals.setdefault(name, {'character_name': name,
                    'first_seen_chapter': idx, 'last_seen_chapter': idx, 'appearance_count': 0})
                current['last_seen_chapter'] = idx
                current['appearance_count'] += count
    finally:
        await result.close()
    return list(totals.values())
