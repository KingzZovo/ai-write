"""Project-scoped pipeline creation and status projections.

Transactional commands live in production.control; execution belongs to
production.runtime. This module never advances runtime state or writes prose.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.models.project import Chapter, Project, Volume


async def create_pipeline(
    db: AsyncSession,
    project_id: str | UUID,
    volume_id: str | UUID | None = None,
) -> PipelineRun:
    """Create a new pipeline run and initialize chapter statuses."""
    project_id = UUID(str(project_id))
    project = await db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError("项目不存在或已删除")

    # Keep project scope in the query even when it has no volumes. An empty
    # volume list must never turn into an unfiltered Chapter SELECT.
    query = (
        select(Chapter)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id)
        .order_by(Volume.volume_idx, Chapter.chapter_idx, Chapter.id)
    )
    if volume_id:
        volume_id = UUID(str(volume_id))
        volume = await db.get(Volume, volume_id)
        if volume is None or volume.project_id != project_id:
            raise ValueError("指定卷不存在或不属于当前项目")
        query = query.where(Chapter.volume_id == volume_id)

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


def pipeline_chapter_query(pipeline_id: str | UUID):
    """One ordering contract for execution and projection; chapter_idx is local.

    Outer joins preserve damaged legacy entries so the worker can report them
    as failures rather than silently lose them from task accounting.
    """
    return (
        select(PipelineChapterStatus)
        .outerjoin(Chapter, PipelineChapterStatus.chapter_id == Chapter.id)
        .outerjoin(Volume, Chapter.volume_id == Volume.id)
        .where(PipelineChapterStatus.pipeline_id == UUID(str(pipeline_id)))
        .order_by(Volume.volume_idx, Chapter.chapter_idx, PipelineChapterStatus.id)
    )


async def get_pipeline_status(
    db: AsyncSession,
    pipeline_id: str | UUID,
) -> dict:
    """Get full pipeline status with per-chapter details."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        raise ValueError("Pipeline not found")

    result = await db.execute(
        pipeline_chapter_query(pipeline.id).add_columns(Chapter.volume_id, Volume.volume_idx, Chapter.chapter_idx)
    )
    chapters = result.all()

    return {
        "id": str(pipeline.id),
        "project_id": str(pipeline.project_id),
        "volume_id": str(pipeline.volume_id) if pipeline.volume_id else None,
        "created_at": pipeline.created_at.isoformat() if pipeline.created_at else None,
        "can_rollback": bool(pipeline.snapshot_json) and pipeline.state not in ("queued", "generating", "pausing"),
        "state": pipeline.state,
        "current_chapter_idx": pipeline.current_chapter_idx,
        "current_chapter_id": next((str(cs.chapter_id) for cs, _, _, _ in chapters if cs.state == "generating"), None),
        "options": pipeline.options_json or {},
        "queued_at": pipeline.queued_at.isoformat() if pipeline.queued_at else None,
        "heartbeat_at": pipeline.heartbeat_at.isoformat() if pipeline.heartbeat_at else None,
        "total_chapters": pipeline.total_chapters,
        "completed_chapters": pipeline.completed_chapters,
        "needs_review_chapters": sum(1 for cs, _, _, _ in chapters if cs.state == "needs_review"),
        "review_round": pipeline.review_round,
        "error_message": pipeline.error_message,
        "started_at": str(pipeline.started_at) if pipeline.started_at else None,
        "completed_at": str(pipeline.completed_at) if pipeline.completed_at else None,
        "chapters": [
            {
                "chapter_id": str(cs.chapter_id),
                "volume_id": str(volume_id) if volume_id else None,
                "volume_idx": volume_idx,
                "chapter_idx": current_idx if current_idx is not None else cs.chapter_idx,
                "state": cs.state,
                "review_round": cs.review_round,
                "attempt_count": cs.attempt_count,
                "generation_task_id": str(cs.generation_task_id) if cs.generation_task_id else None,
                "word_count": cs.word_count,
                "quality_score": cs.quality_score,
                "error_message": cs.error_message,
            }
            for cs, volume_id, volume_idx, current_idx in chapters
        ],
    }
