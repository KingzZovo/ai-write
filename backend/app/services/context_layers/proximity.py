"""Layer 1: Proximity — recent chapter summaries, current content, outlines."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Outline, Volume, VolumeSummary

logger = logging.getLogger(__name__)


async def build_proximity_layer(
    pack,
    project_id: str | UUID,
    volume_id: str | UUID,
    chapter_idx: int,
    db: AsyncSession,
) -> None:
    """Build Layer 1: recent summaries, current content, outlines."""
    pid = str(project_id)
    vid = str(volume_id)

    try:
        # Get last 5 chapter summaries
        result = await db.execute(
            select(Chapter.summary, Chapter.chapter_idx)
            .where(
                Chapter.volume_id == vid,
                Chapter.chapter_idx < chapter_idx,
                Chapter.summary.isnot(None),
                Chapter.summary != "",
            )
            .order_by(Chapter.chapter_idx.desc())
            .limit(5)
        )
        rows = result.all()
        pack.recent_summaries = [
            f"[第{row.chapter_idx}章] {row.summary}"
            for row in reversed(rows)
        ]

        # Get current chapter content
        current_result = await db.execute(
            select(Chapter)
            .where(
                Chapter.volume_id == vid,
                Chapter.chapter_idx == chapter_idx,
            )
        )
        current_chapter = current_result.scalar_one_or_none()
        if current_chapter:
            pack.current_content = current_chapter.content_text or ""
            pack.current_outline = current_chapter.outline_json or {}

        # Get next 10 chapters outline direction
        future_result = await db.execute(
            select(Chapter.chapter_idx, Chapter.title, Chapter.outline_json)
            .where(
                Chapter.volume_id == vid,
                Chapter.chapter_idx > chapter_idx,
            )
            .order_by(Chapter.chapter_idx.asc())
            .limit(10)
        )
        for row in future_result.all():
            outline_summary = ""
            if row.outline_json:
                oj = row.outline_json
                if isinstance(oj, dict):
                    outline_summary = oj.get("summary", "") or oj.get(
                        "main_plot", ""
                    )
                elif isinstance(oj, str):
                    outline_summary = oj
            direction = f"第{row.chapter_idx}章《{row.title or ''}》: {outline_summary}"
            pack.future_outlines.append(direction)

        # v1.7.4 P0-1: load book + volume outline
        try:
            book_outline_q = await db.execute(
                select(Outline.content_json)
                .where(Outline.project_id == pid, Outline.level == "book")
                .order_by(Outline.version.desc())
                .limit(1)
            )
            bo = book_outline_q.scalar_one_or_none()
            if isinstance(bo, dict):
                raw = bo.get("raw_text") or bo.get("summary") or ""
                if isinstance(raw, str) and raw.strip():
                    if len(raw) > 2200:
                        pack.book_outline_excerpt = (
                            raw[:1500].rstrip()
                            + "\n\n…(中部省略)…\n\n"
                            + raw[-500:].lstrip()
                        )
                    else:
                        pack.book_outline_excerpt = raw
        except Exception as e:
            logger.warning("Failed to load book outline: %s", e)

        try:
            vol_outline_q = await db.execute(
                select(Outline.content_json)
                .where(
                    Outline.project_id == pid,
                    Outline.level == "volume",
                )
                .order_by(Outline.version.desc())
            )
            for vo_row in vol_outline_q.scalars().all():
                if isinstance(vo_row, dict) and (
                    vo_row.get("volume_idx") is None
                    or str(vo_row.get("volume_id", vid)) == vid
                ):
                    pack.volume_outline = vo_row
                    break
            if not pack.volume_outline:
                fallback = await db.execute(
                    select(Outline.content_json)
                    .where(
                        Outline.project_id == pid,
                        Outline.level == "volume",
                    )
                    .order_by(Outline.version.desc())
                    .limit(1)
                )
                fb = fallback.scalar_one_or_none()
                if isinstance(fb, dict):
                    pack.volume_outline = fb
        except Exception as e:
            logger.warning("Failed to load volume outline: %s", e)

        # Also try to get summaries from previous volumes if at start of volume
        if chapter_idx <= 3:
            vol_summary_result = await db.execute(
                select(VolumeSummary.summary_text)
                .join(Volume, VolumeSummary.volume_id == Volume.id)
                .where(
                    Volume.project_id == pid,
                    Volume.id != vid,
                )
                .order_by(Volume.volume_idx.desc())
                .limit(2)
            )
            vol_summaries = vol_summary_result.scalars().all()
            for vs in reversed(list(vol_summaries)):
                pack.recent_summaries.insert(0, f"[前卷摘要] {vs}")

    except Exception as e:
        logger.warning("Failed to build proximity layer: %s", e)
