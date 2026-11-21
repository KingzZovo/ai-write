"""Transactional commands. Callers commit before publishing any broker message.

All functions deliberately avoid model calls and Celery; a committed queued
row is the durable outbox and remains recoverable after broker failure.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services.production.locking import try_command_lock

ACTIVE_STATES = ("queued", "generating", "pausing")


class PipelineConflict(ValueError):
    """The command would overlap an active writer or discard another edit."""


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def _pipeline(db: AsyncSession, pipeline_id: str | UUID, *, lock=False) -> PipelineRun:
    query = select(PipelineRun).where(PipelineRun.id == UUID(str(pipeline_id))).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    result = (await db.execute(query)).scalar_one_or_none()
    if result is None:
        raise ValueError("整书任务不存在")
    return result


async def _project_command(db: AsyncSession, pipeline_id: str | UUID) -> PipelineRun:
    pipeline = await _pipeline(db, pipeline_id)
    if not await try_command_lock(db, pipeline.project_id):
        raise PipelineConflict("当前章节仍在写入，请等待章节结束后再操作")
    return await _pipeline(db, pipeline_id, lock=True)


async def request_start(db: AsyncSession, pipeline_id: str | UUID) -> tuple[PipelineRun, bool]:
    pipeline = await _pipeline(db, pipeline_id)
    if pipeline.state in ACTIVE_STATES:
        return pipeline, False
    pipeline = await _project_command(db, pipeline_id)
    if pipeline.state in ACTIVE_STATES:
        return pipeline, False
    project = await db.get(Project, pipeline.project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError("项目不存在或已删除")
    if pipeline.state == "completed":
        return pipeline, False
    other = await db.scalar(select(PipelineRun.id).where(
        PipelineRun.project_id == pipeline.project_id,
        PipelineRun.id != pipeline.id, PipelineRun.state.in_(ACTIVE_STATES),
    ).limit(1))
    if other is not None:
        raise PipelineConflict("本书已有进行中的整书任务，请先暂停该任务")
    statuses = list((await db.scalars(select(PipelineChapterStatus).where(
        PipelineChapterStatus.pipeline_id == pipeline.id,
    ))).all())
    if not statuses:
        raise ValueError("任务没有章节，请重新创建整书任务")
    current_query = select(Chapter.id).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == pipeline.project_id)
    if pipeline.volume_id is not None:
        current_query = current_query.where(Volume.id == pipeline.volume_id)
    current_ids = set((await db.scalars(current_query.execution_options(autoflush=False))).all())
    status_ids = {status.chapter_id for status in statuses}
    if not status_ids.issubset(current_ids):
        raise ValueError("任务包含不属于当前作品或分卷的章节，或章节已删除，请重新创建任务")
    if current_ids != status_ids or len(statuses) != pipeline.total_chapters or len(status_ids) != len(statuses):
        raise ValueError("章节清单已改变，请核对当前卷章并重新创建任务")
    if pipeline.snapshot_json is None:
        snapshot = {}
        for status in statuses:
            chapter = await db.get(Chapter, status.chapter_id)
            volume = await db.get(Volume, chapter.volume_id) if chapter else None
            if volume is None or volume.project_id != pipeline.project_id or (
                pipeline.volume_id is not None and pipeline.volume_id != volume.id
            ):
                raise ValueError("任务包含不属于当前作品或分卷的章节")
            snapshot[str(chapter.id)] = {
                "content_text": chapter.content_text or "", "status": chapter.status,
                "word_count": chapter.word_count, "summary": chapter.summary,
                "summary_source_hash": chapter.summary_source_hash,
                "updated_at": chapter.updated_at.isoformat() if chapter.updated_at else None,
                "task_state": status.state,
            }
        pipeline.snapshot_json = snapshot
    for status in statuses:
        if status.state in ("failed", "needs_review", "generating", "reviewing"):
            status.state = "pending"
            status.attempt_count = 0  # A new explicit command gets its own retry budget.
            status.error_message = None
            status.started_at = status.completed_at = None
    pipeline.state = "queued"
    pipeline.command_id = uuid4()
    pipeline.worker_id = None
    pipeline.queued_at = datetime.now(timezone.utc)
    pipeline.dispatched_at = pipeline.heartbeat_at = None
    pipeline.error_message = None
    pipeline.completed_at = None
    await db.flush()
    return pipeline, True


async def request_pause(db: AsyncSession, pipeline_id: str | UUID) -> PipelineRun:
    # Pause must remain callable while a worker owns the project mutex.
    pipeline = await _pipeline(db, pipeline_id, lock=True)
    if pipeline.state == "queued":
        pipeline.state = "paused"
        pipeline.command_id = uuid4()  # Invalidate already-delivered messages.
        pipeline.worker_id = None
    elif pipeline.state == "generating":
        pipeline.state = "pausing"
    await db.flush()
    return pipeline


async def recover_abandoned(db: AsyncSession, pipeline_id: str | UUID, *, stale_seconds=90) -> bool:
    pipeline = await _pipeline(db, pipeline_id)
    if pipeline.state not in ("generating", "pausing"):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    if pipeline.heartbeat_at is not None and pipeline.heartbeat_at > cutoff:
        return False
    if not await try_command_lock(db, pipeline.project_id):
        return False  # A slow/live worker still owns the connection.
    pipeline = await _pipeline(db, pipeline_id, lock=True)
    if pipeline.state not in ("generating", "pausing") or (
        pipeline.heartbeat_at is not None and pipeline.heartbeat_at > cutoff
    ):
        return False
    pipeline.state = "paused" if pipeline.state == "pausing" else "queued"
    pipeline.command_id = uuid4()
    pipeline.worker_id = None
    pipeline.heartbeat_at = pipeline.dispatched_at = None
    pipeline.queued_at = datetime.now(timezone.utc)
    pipeline.error_message = "检测到执行进程中断，已保留正文和检查点"
    await db.flush()
    return True


async def request_rollback(db: AsyncSession, pipeline_id: str | UUID) -> PipelineRun:
    pipeline = await _project_command(db, pipeline_id)
    if pipeline.state in ACTIVE_STATES:
        raise PipelineConflict("请先暂停整书任务，再恢复生成前的版本")
    if not pipeline.snapshot_json:
        raise ValueError("没有可恢复的生成前版本")
    statuses = list((await db.scalars(select(PipelineChapterStatus).where(
        PipelineChapterStatus.pipeline_id == pipeline.id,
    ))).all())
    by_chapter = {str(row.chapter_id): row for row in statuses}
    if set(pipeline.snapshot_json) != set(by_chapter) or any(
        not isinstance(value, dict) for value in pipeline.snapshot_json.values()
    ):
        raise PipelineConflict("快照与当前作品章节不一致，无法自动恢复")
    restore = []
    # Validate every chapter before mutating any. Locks serialize manual edits
    # with the final hash check and update in the same transaction.
    for chapter_id, baseline in pipeline.snapshot_json.items():
        status = by_chapter.get(chapter_id)
        chapter = await db.scalar(select(Chapter).where(Chapter.id == UUID(chapter_id)).with_for_update().execution_options(populate_existing=True))
        volume = await db.get(Volume, chapter.volume_id) if chapter else None
        if status is None or volume is None or volume.project_id != pipeline.project_id or (
            pipeline.volume_id is not None and pipeline.volume_id != volume.id
        ):
            raise PipelineConflict("快照与当前作品章节不一致，无法自动恢复")
        expected = status.saved_hash or content_hash(baseline.get("content_text"))
        expected_version = status.saved_updated_at
        if expected_version is None and baseline.get("updated_at"):
            expected_version = datetime.fromisoformat(baseline["updated_at"])
        if content_hash(chapter.content_text) != expected or (
            expected_version is not None and chapter.updated_at != expected_version
        ):
            raise PipelineConflict("正文在任务之外发生了修改，已保留当前内容，请先检查版本")
        restore.append((chapter, status, baseline))
    for chapter, status, baseline in restore:
        chapter.content_text = baseline.get("content_text", "")
        chapter.status = baseline.get("status", "draft")
        chapter.word_count = baseline.get("word_count", 0)
        chapter.summary = baseline.get("summary")
        chapter.summary_source_hash = baseline.get("summary_source_hash")
        status.state = baseline.get("task_state", "pending")
        status.word_count = chapter.word_count
        status.saved_hash = None
        status.saved_updated_at = None
        status.generation_task_id = None
        status.attempt_count = status.review_round = 0
        status.quality_score = None
        status.error_message = None
        status.started_at = status.completed_at = None
    pipeline.state = "planning"
    pipeline.snapshot_json = None  # Consumed: a new run must snapshot the restored versions.
    pipeline.command_id = uuid4()
    pipeline.worker_id = None
    pipeline.completed_chapters = sum(row.state == "completed" for row in statuses)
    pipeline.current_chapter_idx = pipeline.review_round = 0
    pipeline.error_message = None
    pipeline.started_at = pipeline.completed_at = None
    pipeline.queued_at = pipeline.dispatched_at = pipeline.heartbeat_at = None
    await db.flush()
    return pipeline
