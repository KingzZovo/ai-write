"""v0.7 — Build a global outline from a reference book's decompile output.

Flow:
  1. Collect BeatSheetCard rows for the given reference book as a three-level
     hierarchical sketch (book > arc > scene).
  2. Feed the sketch + user wizard parameters into the existing outline_book
     prompt (task_type="outline_book").
  3. Return the generated outline text; callers may then run the normal
     settings_extractor pipeline to harvest characters / world rules.

No raw passages are sent to the LLM — only abstracted beats — so proper nouns
from the reference book are not leaked.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decompile import BeatSheetCard, ReferenceBookSlice
from app.models.project import ReferenceBook
from app.services.prompt_registry import run_text_prompt

logger = logging.getLogger(__name__)


async def _load_beat_sketch(book_id: str, db: AsyncSession) -> str:
    rows = await db.execute(
        select(BeatSheetCard, ReferenceBookSlice)
        .join(ReferenceBookSlice, BeatSheetCard.slice_id == ReferenceBookSlice.id)
        .where(BeatSheetCard.book_id == book_id)
        .order_by(ReferenceBookSlice.sequence_id.asc())
    )
    lines: list[str] = []
    for card, slc in rows.all():
        beat = card.beat_json or {}
        scene_type = beat.get("scene_type") or "unknown"
        outcome = beat.get("outcome") or ""
        pattern = beat.get("reusable_pattern") or beat.get("summary") or ""
        chap = slc.chapter_idx if slc.chapter_idx is not None else "-"
        lines.append(f"[ch{chap}/{slc.sequence_id}] {scene_type}: {pattern} => {outcome}")
    return "\n".join(lines)


async def build_outline_from_reference(
    *,
    reference_book_id: str,
    wizard_params: dict[str, Any],
    db: AsyncSession,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a global outline from the given reference book's decompile output."""
    book = await db.get(ReferenceBook, reference_book_id)
    if book is None:
        return {"status": "error", "reason": "reference_book_not_found"}

    sketch = await _load_beat_sketch(reference_book_id, db)
    if not sketch:
        return {
            "status": "error",
            "reason": "no_beat_sheets",
            "hint": "Run POST /api/reference-books/{id}/reprocess first",
        }

    user_intent = wizard_params.get("intent") or wizard_params.get("description") or ""
    target_volumes = wizard_params.get("target_volumes") or 5
    target_chapters_per_volume = wizard_params.get("target_chapters_per_volume") or 30
    style_hint = wizard_params.get("style_hint") or ""

    user_content = (
        f"<用户向导>\n{user_intent}\n</用户向导>\n\n"
        f"<模仿风格>\n{style_hint}\n</模仿风格>\n\n"
        f"<结构目标>\n圈数 ≈ {target_volumes}，每卷章数 ≈ {target_chapters_per_volume}\n</结构目标>\n\n"
        f"<参考书骨架摘要>\n{sketch}\n</参考书骨架摘要>\n\n"
        "请在不泄露参考书专名的前提下，输出全新一本书的全局大纲。"
    )

    try:
        result = await run_text_prompt(
            "outline_book",
            user_content,
            db,
            project_id=project_id,
        )
    except Exception as exc:
        logger.exception("outline_book prompt failed")
        return {"status": "error", "reason": "prompt_failed", "detail": str(exc)}

    outline_text = getattr(result, "text", "") or str(result or "")
    return {
        "status": "ok",
        "reference_book": {"id": str(book.id), "title": book.title},
        "outline_text": outline_text,
        "sketch_line_count": sketch.count("\n") + 1,
    }
