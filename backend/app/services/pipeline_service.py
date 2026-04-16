"""Production Pipeline Service — state machine for full-book generation.

Pipeline states: planning → generating → reviewing → polishing → completed

Features:
- Per-chapter state tracking
- Review cycles (up to 3 rounds)
- Snapshot before generation (rollback on failure)
- Checkpoint resume (pick up from last failed chapter)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.models.project import Chapter, Volume

logger = logging.getLogger(__name__)


async def create_pipeline(
    db: AsyncSession,
    project_id: str | UUID,
    volume_id: str | UUID | None = None,
) -> PipelineRun:
    """Create a new pipeline run and initialize chapter statuses."""
    # Get chapters
    query = select(Chapter).order_by(Chapter.chapter_idx)
    if volume_id:
        query = query.where(Chapter.volume_id == str(volume_id))
    else:
        # Get all chapters for the project via volumes
        vol_ids = await db.execute(
            select(Volume.id).where(Volume.project_id == str(project_id))
        )
        vids = [str(v) for v in vol_ids.scalars().all()]
        if vids:
            query = query.where(Chapter.volume_id.in_(vids))

    result = await db.execute(query)
    chapters = list(result.scalars().all())

    if not chapters:
        raise ValueError("没有可生成的章节，请先创建章节大纲")

    # Create pipeline run
    pipeline = PipelineRun(
        project_id=str(project_id),
        volume_id=str(volume_id) if volume_id else None,
        state="planning",
        total_chapters=len(chapters),
    )
    db.add(pipeline)
    await db.flush()

    # Create per-chapter statuses
    for ch in chapters:
        status = PipelineChapterStatus(
            pipeline_id=pipeline.id,
            chapter_id=ch.id,
            chapter_idx=ch.chapter_idx,
            state="pending",
        )
        db.add(status)

    await db.flush()
    await db.refresh(pipeline)
    return pipeline


async def advance_pipeline(
    db: AsyncSession,
    pipeline_id: str | UUID,
) -> PipelineRun:
    """Advance the pipeline state machine. Returns updated pipeline."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        raise ValueError("Pipeline not found")

    if pipeline.state == "completed":
        return pipeline

    # Count chapter states
    result = await db.execute(
        select(PipelineChapterStatus)
        .where(PipelineChapterStatus.pipeline_id == pipeline.id)
        .order_by(PipelineChapterStatus.chapter_idx)
    )
    chapter_statuses = list(result.scalars().all())

    completed = sum(1 for cs in chapter_statuses if cs.state == "completed")
    failed = sum(1 for cs in chapter_statuses if cs.state == "failed")

    pipeline.completed_chapters = completed

    # State transitions
    if pipeline.state == "planning":
        pipeline.state = "generating"
        pipeline.started_at = datetime.now(timezone.utc)

    elif pipeline.state == "generating":
        if completed + failed >= pipeline.total_chapters:
            if failed > 0 and pipeline.review_round < pipeline.max_review_rounds:
                pipeline.state = "reviewing"
                pipeline.review_round += 1
            elif failed > 0:
                pipeline.state = "failed"
                pipeline.error_message = f"{failed} 章生成失败（已达最大重试次数）"
            else:
                pipeline.state = "polishing"

    elif pipeline.state == "reviewing":
        # Reset failed chapters to pending for retry
        for cs in chapter_statuses:
            if cs.state == "failed":
                cs.state = "pending"
                cs.error_message = None
        pipeline.state = "generating"

    elif pipeline.state == "polishing":
        pipeline.state = "completed"
        pipeline.completed_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(pipeline)
    return pipeline


async def get_pipeline_status(
    db: AsyncSession,
    pipeline_id: str | UUID,
) -> dict:
    """Get full pipeline status with per-chapter details."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        raise ValueError("Pipeline not found")

    result = await db.execute(
        select(PipelineChapterStatus)
        .where(PipelineChapterStatus.pipeline_id == pipeline.id)
        .order_by(PipelineChapterStatus.chapter_idx)
    )
    chapters = result.scalars().all()

    return {
        "id": str(pipeline.id),
        "project_id": str(pipeline.project_id),
        "state": pipeline.state,
        "current_chapter_idx": pipeline.current_chapter_idx,
        "total_chapters": pipeline.total_chapters,
        "completed_chapters": pipeline.completed_chapters,
        "review_round": pipeline.review_round,
        "error_message": pipeline.error_message,
        "started_at": str(pipeline.started_at) if pipeline.started_at else None,
        "completed_at": str(pipeline.completed_at) if pipeline.completed_at else None,
        "chapters": [
            {
                "chapter_idx": cs.chapter_idx,
                "state": cs.state,
                "review_round": cs.review_round,
                "word_count": cs.word_count,
                "quality_score": cs.quality_score,
                "error_message": cs.error_message,
            }
            for cs in chapters
        ],
    }


async def snapshot_pipeline(db: AsyncSession, pipeline_id: str | UUID) -> None:
    """Save a snapshot of current chapter contents for rollback."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        return

    result = await db.execute(
        select(PipelineChapterStatus)
        .where(PipelineChapterStatus.pipeline_id == pipeline.id)
    )
    statuses = result.scalars().all()

    snapshot = {}
    for cs in statuses:
        chapter = await db.get(Chapter, str(cs.chapter_id))
        if chapter:
            snapshot[str(cs.chapter_id)] = {
                "content_text": chapter.content_text or "",
                "status": chapter.status,
                "word_count": chapter.word_count,
            }

    pipeline.snapshot_json = snapshot
    await db.flush()


async def rollback_pipeline(db: AsyncSession, pipeline_id: str | UUID) -> bool:
    """Restore chapter contents from snapshot. Returns True if successful."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline or not pipeline.snapshot_json:
        return False

    for chapter_id, data in pipeline.snapshot_json.items():
        chapter = await db.get(Chapter, chapter_id)
        if chapter:
            chapter.content_text = data.get("content_text", "")
            chapter.status = data.get("status", "draft")
            chapter.word_count = data.get("word_count", 0)

    pipeline.state = "planning"
    pipeline.completed_chapters = 0
    pipeline.error_message = None
    await db.flush()
    return True
