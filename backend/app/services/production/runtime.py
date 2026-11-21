"""Claim, heartbeat, execute and finalize durable whole-book commands.

The DB command token fences stale deliveries, a dedicated project connection
excludes overlapping workers, and the canonical chapter adapter fences writes.
No database transaction remains open during model generation.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import logging
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.services.pipeline_service import pipeline_chapter_query
from app.services.production.chapter_executor import ChapterOutcome, execute_chapter
from app.services.production.fencing import StaleChapterWrite
from app.services.production.locking import project_worker_lock

logger = logging.getLogger(__name__)


class OwnershipLost(RuntimeError):
    pass


def _owned(pipeline, command_id, worker_id):
    return pipeline is not None and pipeline.command_id == command_id and pipeline.worker_id == worker_id


async def _locked_pipeline(db, pipeline_id):
    return await db.scalar(select(PipelineRun).where(PipelineRun.id == pipeline_id).with_for_update())


async def _finish(db, pipeline):
    counts = Counter(dict((await db.execute(select(
        PipelineChapterStatus.state, func.count(),
    ).where(PipelineChapterStatus.pipeline_id == pipeline.id).group_by(PipelineChapterStatus.state))).all()))
    pipeline.completed_chapters = counts["completed"]
    pipeline.worker_id = None
    if pipeline.state == "pausing":
        pipeline.state = "paused"
        return
    if sum(counts.values()) != pipeline.total_chapters:
        pipeline.state = "failed"
        pipeline.error_message = "章节清单已改变，请检查作品章节后重新创建任务"
    elif counts["failed"]:
        pipeline.state = "failed"
        pipeline.error_message = f"{counts['failed']} 章未能完成，已保存可用正文和检查点"
    elif counts["needs_review"]:
        pipeline.state = "needs_review"
        pipeline.error_message = f"{counts['needs_review']} 章草稿待审核，其余章节已处理"
    elif counts["completed"] == pipeline.total_chapters:
        pipeline.state = "completed"
        pipeline.error_message = None
    else:
        pipeline.state = "failed"
        pipeline.error_message = "章节任务状态不完整，请恢复后继续"
    pipeline.completed_at = datetime.now(timezone.utc)


async def _heartbeat(sessions, lease, pipeline_id, command_id, worker_id, interval):
    while True:
        await asyncio.sleep(interval)
        await lease.check()
        async with sessions() as db:
            result = await db.execute(update(PipelineRun).where(
                PipelineRun.id == pipeline_id, PipelineRun.command_id == command_id,
                PipelineRun.worker_id == worker_id, PipelineRun.state.in_(("generating", "pausing")),
            ).values(heartbeat_at=datetime.now(timezone.utc)))
            await db.commit()
            if result.rowcount != 1:
                raise OwnershipLost("整书任务执行权已更新")


async def _chapters(sessions, pipeline_id, command_id, worker_id, executor):
    while True:
        async with sessions() as db:
            pipeline = await _locked_pipeline(db, pipeline_id)
            if not _owned(pipeline, command_id, worker_id):
                return
            if pipeline.state == "pausing":
                await _finish(db, pipeline)
                await db.commit()
                return
            if pipeline.state != "generating":
                return
            status = await db.scalar(pipeline_chapter_query(pipeline_id).where(
                PipelineChapterStatus.state.in_(("pending", "generating")),
            ).limit(1))
            if status is None:
                await _finish(db, pipeline)
                await db.commit()
                return
            status_id, attempts_before = status.id, status.attempt_count or 0
            try:
                max_attempts = max(1, min(5, int((pipeline.options_json or {}).get("max_attempts", 2))))
            except (TypeError, ValueError, OverflowError):
                max_attempts = 2  # Legacy malformed options must not strand recovery.
            await db.commit()
        try:
            outcome = await executor(sessions, pipeline_id, command_id, worker_id, status_id)
        except StaleChapterWrite as exc:
            outcome = ChapterOutcome("failed", error=str(exc))
        except Exception:
            logger.exception("Chapter execution failed pipeline=%s chapter_status=%s", pipeline_id, status_id)
            outcome = ChapterOutcome("failed", error="章节执行发生异常，正文与检查点已保留", retryable=True)
        async with sessions() as db:
            pipeline = await _locked_pipeline(db, pipeline_id)
            if not _owned(pipeline, command_id, worker_id):
                return
            status = await db.get(PipelineChapterStatus, status_id)
            if status is None:
                continue
            # Pause may arrive between selection and execution: the adapter
            # rejects new work in pausing, leaving the pending chapter intact.
            if pipeline.state == "pausing" and status.state == "pending":
                await _finish(db, pipeline)
                await db.commit()
                return
            status.state = outcome.state
            status.word_count = outcome.word_count
            status.error_message = outcome.error
            if outcome.state == "failed":
                status.attempt_count = max(status.attempt_count or 0, attempts_before + 1)
                pipeline.last_error_chapter = status.chapter_idx
                if outcome.retryable and status.attempt_count < max_attempts:
                    status.state = "pending"
            status.completed_at = None if status.state == "pending" else datetime.now(timezone.utc)
            pipeline.completed_chapters = await db.scalar(select(func.count()).select_from(
                PipelineChapterStatus,
            ).where(PipelineChapterStatus.pipeline_id == pipeline_id, PipelineChapterStatus.state == "completed"))
            await db.commit()


async def run_command(engine, sessions, pipeline_id, command_id, *, executor=execute_chapter, heartbeat_seconds=15) -> bool:
    """Return False for a stale/duplicate delivery; no model calls in that case."""
    pipeline_id, command_id = UUID(str(pipeline_id)), UUID(str(command_id))
    async with sessions() as db:
        pipeline = await db.get(PipelineRun, pipeline_id)
        if pipeline is None or pipeline.state != "queued" or pipeline.command_id != command_id:
            return False
        project_id = pipeline.project_id
    async with project_worker_lock(engine, project_id) as lease:
        if not lease:
            return False
        worker_id = uuid4()
        async with sessions() as db:
            pipeline = await _locked_pipeline(db, pipeline_id)
            if pipeline is None or pipeline.state != "queued" or pipeline.command_id != command_id:
                return False
            pipeline.worker_id, pipeline.state = worker_id, "generating"
            pipeline.heartbeat_at = datetime.now(timezone.utc)
            pipeline.started_at = pipeline.started_at or pipeline.heartbeat_at
            pipeline.error_message = None
            await db.commit()
        work = asyncio.create_task(_chapters(sessions, pipeline_id, command_id, worker_id, executor))
        heartbeat = asyncio.create_task(_heartbeat(sessions, lease, pipeline_id, command_id, worker_id, heartbeat_seconds))
        try:
            done, _ = await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await heartbeat
        finally:
            # Drain both children before releasing the project lock. A killed
            # worker leaves a checkpoint that Beat can safely recover later.
            for task in (work, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
        return True
