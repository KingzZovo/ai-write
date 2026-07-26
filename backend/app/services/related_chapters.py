"""Deterministic related-chapter recall (F4 / ainovel).

Adapted from voocel/ainovel-cli's structured related-chapter lookup (design
idea; wording our own).

Complements the semantic (Qdrant) RAG path with a *deterministic* one: given
the current chapter outline, look back through already-persisted structured
signals -- foreshadow planting chapters, secondary-cast last-seen chapters,
recent character-state changes -- and tell the writer which historical chapters
to re-read for consistency. Zero embeddings, explainable, and it won't miss a
causally-related chapter just because it's semantically dissimilar (e.g. the
chapter that planted a foreshadow).

Signal coverage (see C3 plan): foreshadow planting (book-global idx),
secondary-cast last_seen, and recent character-state changes
(character_states.chapter_start, book-global since entity_tasks moved to the
global chapter axis) are usable; foreshadow *advancement* has no record
(planting only); the relationship projection has no chapter column so that
path is still skipped.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def rank_related_chapters(
    total_chapters: int,
    foreshadow_hits: list[dict],
    character_hits: list[dict],
    state_hits: list[dict],
    *,
    min_total_chapters: int = 30,
    min_signals: int = 3,
    top_k: int = 6,
) -> list[dict]:
    """Pure ranking + restraint gates over pre-fetched structured signals.

    Each ``*_hits`` item is ``{"chapter": int, "reason": str}`` (summary added
    later by the caller). Restraint:
    - books with <= ``min_total_chapters`` written chapters: return [] (too
      short for look-back to matter),
    - fewer than ``min_signals`` total signals: return [] ("too few is worse
      than none"),
    - dedup by chapter (merge reasons), rank by signal count then recency,
      cap at ``top_k``.
    """
    if total_chapters <= min_total_chapters:
        return []

    all_hits = list(foreshadow_hits) + list(character_hits) + list(state_hits)
    if len(all_hits) < min_signals:
        return []

    by_chapter: dict[int, dict] = {}
    for hit in all_hits:
        ch = hit.get("chapter")
        if ch is None:
            continue
        entry = by_chapter.setdefault(ch, {"chapter": ch, "reasons": [], "signals": 0})
        reason = hit.get("reason", "")
        if reason and reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        entry["signals"] += 1

    ranked = sorted(
        by_chapter.values(),
        key=lambda e: (-e["signals"], -e["chapter"]),
    )
    return ranked[:top_k]


async def find_related_chapters(
    db,
    project_id,
    volume_id,
    chapter_idx,
    outline_json: dict,
    *,
    min_total_chapters: int = 30,
    min_signals: int = 2,
    state_window: int = 2,
) -> list[dict]:
    """Look back through structured signals for chapters related to this one.

    ``chapter_idx`` is the BOOK-GLOBAL index of the chapter being generated
    (same axis as ``Foreshadow.planted_chapter`` and
    ``character_states.chapter_start``).

    Returns ranked ``[{chapter, reasons, signals, summary}]`` or [] when the
    restraint gates fire. Fail-safe: any error returns [] (never blocks build).
    """
    try:
        from sqlalchemy import func, select

        from app.models.project import (
            Chapter,
            Character,
            CharacterAppearance,
            CharacterState,
            Foreshadow,
            Volume,
        )
        from app.services.character_roster import outline_tokens

        # Total written chapters (book-global) for the min-total gate.
        total = (
            await db.execute(
                select(func.count(Chapter.id))
                .select_from(Chapter)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == str(project_id),
                    Chapter.content_text.isnot(None),
                    Chapter.content_text != "",
                )
            )
        ).scalar() or 0
        if total <= min_total_chapters:
            return []

        # Build the outline text + entity-name set for matching.
        combined = []
        for key in ("summary", "main_plot", "key_events", "events", "description"):
            v = outline_json.get(key) if isinstance(outline_json, dict) else None
            if isinstance(v, str):
                combined.append(v)
            elif isinstance(v, list):
                combined.append(" ".join(str(x) for x in v))
        outline_text = " ".join(combined)
        tokens = outline_tokens(outline_text)

        # Path 1: foreshadows whose description overlaps the outline -> planted_chapter.
        foreshadow_hits: list[dict] = []
        fs_rows = (
            await db.execute(
                select(Foreshadow.description, Foreshadow.planted_chapter)
                .where(
                    Foreshadow.project_id == str(project_id),
                    Foreshadow.status != "resolved",
                )
            )
        ).all()
        for desc, planted in fs_rows:
            if planted is None or not desc:
                continue
            if _overlaps(desc, outline_text, tokens):
                foreshadow_hits.append(
                    {"chapter": int(planted), "reason": f"伏笔「{desc[:18]}」埋设"}
                )

        # Path 2: outline-mentioned secondary cast -> their last_seen_chapter.
        character_hits: list[dict] = []
        if tokens:
            cast_rows = (
                await db.execute(
                    select(
                        CharacterAppearance.character_name,
                        CharacterAppearance.last_seen_chapter,
                    ).where(CharacterAppearance.project_id == str(project_id))
                )
            ).all()
            for name, last_seen in cast_rows:
                if last_seen is None or not name:
                    continue
                if name in outline_text:
                    character_hits.append(
                        {"chapter": int(last_seen), "reason": f"角色「{name}」上次出场"}
                    )

        # Path 3: recent character-state changes. Re-enabled now that
        # character_states.chapter_start is book-global (entity_tasks writes
        # on the global axis; pre-existing rows all came from volume 1 where
        # global == local), so the look-back window can no longer collide
        # with other volumes' local indices.
        state_hits: list[dict] = []
        try:
            st_rows = (
                await db.execute(
                    select(Character.name, CharacterState.chapter_start)
                    .select_from(CharacterState)
                    .join(Character, Character.id == CharacterState.character_id)
                    .where(
                        CharacterState.project_id == str(project_id),
                        CharacterState.chapter_start >= int(chapter_idx) - state_window,
                        CharacterState.chapter_start < int(chapter_idx),
                    )
                )
            ).all()
            for name, cs in st_rows:
                if cs is None or not name:
                    continue
                state_hits.append(
                    {"chapter": int(cs), "reason": f"角色「{name}」状态最近变化"}
                )
        except Exception as e:
            logger.debug("character-state signal skipped: %s", e)

        ranked = rank_related_chapters(
            total,
            foreshadow_hits,
            character_hits,
            state_hits,
            min_total_chapters=min_total_chapters,
            min_signals=min_signals,
        )
        if not ranked:
            return []

        # Attach a short summary per chapter (book-global idx best-effort).
        for entry in ranked:
            entry["summary"] = await _chapter_summary(db, project_id, entry["chapter"])
        return ranked
    except Exception as e:
        logger.warning("find_related_chapters failed: %s", e)
        return []


def _overlaps(description: str, outline_text: str, tokens: set[str]) -> bool:
    """True if foreshadow description and outline share meaningful content."""
    if not description or not outline_text:
        return False
    head = description[:24]
    if head and head in outline_text:
        return True
    # Any 4+ char outline token contained in the description.
    return any(tok in description for tok in tokens)


async def _chapter_summary(db, project_id, global_idx: int) -> str:
    """Best-effort short summary for a book-global chapter index (<=80 chars)."""
    try:
        from sqlalchemy import select

        from app.models.project import Chapter, Volume

        # Chapter.global_idx is the canonical book-global axis (backfilled by
        # migration a1001915, stamped on insert) — direct lookup.
        row = (
            await db.execute(
                select(Chapter.summary, Chapter.title)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == str(project_id),
                    Chapter.global_idx == int(global_idx),
                )
                .limit(1)
            )
        ).first()
        if row:
            summary, title = row
            text = (summary or title or "").strip()
            return text[:80]
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("_chapter_summary failed for idx=%s: %s", global_idx, e)
    return ""


def render_recall_block(items: list[dict], max_chars: int = 600) -> str:
    """Render the deterministic related-chapter recall block. Empty -> ""."""
    if not items:
        return ""
    lines = [
        "【相关历史章回读（确定性反查）】",
        "本章与以下历史章节强相关，正文须与其设定/伏笔/人物状态对齐：",
    ]
    for it in items:
        reasons = "；".join(it.get("reasons", [])) or "相关"
        summary = it.get("summary") or ""
        suffix = f"（{summary}）" if summary else ""
        lines.append(f"- [CH-{it['chapter']}]：{reasons}{suffix}")

    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
    if len(out) <= 2:  # only headers fit -> no real signal
        return ""
    return "\n".join(out)
