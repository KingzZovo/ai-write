"""Fence canonical chapter writes against stale workers and concurrent edits.

Only sessions inside chapter_write_fence are affected. Checks and chapter
updates use the SAME transaction, with row locks held until commit. Losing a
worker token cannot be hidden by the legacy generator's salvage-write paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Volume
from app.services.production.control import content_hash
from app.services.batch_execution import current_batch_execution, protect_batch_write


class StaleChapterWrite(RuntimeError):
    pass


@dataclass
class WriteFence:
    pipeline_id: UUID | None
    command_id: UUID | None
    worker_id: UUID | None
    chapter_id: UUID
    expected_updated_at: datetime | None
    touched_sessions: set = field(default_factory=set)
    active: bool = True
    project_id: UUID | None = None
    generation_task_id: UUID | None = None


_fence: ContextVar[WriteFence | None] = ContextVar("production_chapter_write_fence", default=None)
_PENDING = "aiwrite_production_pending_version"
_WRITING = "aiwrite_production_writing_chapter"
_BATCH_WRITING = "aiwrite_batch_writing_chapter"
_TASK_WRITING = "aiwrite_independent_writing_chapter"


@contextmanager
def chapter_write_fence(pipeline_id, command_id, worker_id, chapter_id, expected_updated_at):
    fence = WriteFence(*(UUID(str(value)) for value in (pipeline_id, command_id, worker_id, chapter_id)), expected_updated_at)
    token = _fence.set(fence)
    try:
        yield fence
    finally:
        fence.active = False
        for session in fence.touched_sessions:
            session.info.pop(_PENDING, None)
            session.info.pop(_WRITING, None)
            session.info.pop(_BATCH_WRITING, None)
            session.info.pop(_TASK_WRITING, None)
        _fence.reset(token)


@contextmanager
def standalone_write_fence(project_id, chapter_id, expected_updated_at, generation_task_id=None):
    fence = WriteFence(None, None, None, UUID(str(chapter_id)), expected_updated_at,
                       project_id=UUID(str(project_id)),
                       generation_task_id=UUID(str(generation_task_id)) if generation_task_id else None)
    token = _fence.set(fence)
    try:
        yield fence
    finally:
        fence.active = False
        for session in fence.touched_sessions:
            session.info.pop(_PENDING, None)
            session.info.pop(_WRITING, None)
            session.info.pop(_BATCH_WRITING, None)
            session.info.pop(_TASK_WRITING, None)
        _fence.reset(token)


def _before_flush(session, flush_context, instances):
    fence = _fence.get()
    if fence is None:
        return
    if not fence.active:
        raise StaleChapterWrite("生成写入上下文已结束，已停止迟到写入")
    from app.services.generation_task_execution import current_task_execution, protect_task_flush
    protect_task_flush(session)
    task_owner = current_task_execution()
    batch_owner = current_batch_execution()
    if batch_owner is not None and (session.dirty or session.new or session.deleted):
        protect_batch_write(session.connection(), fence.generation_task_id)
    if fence.pipeline_id is None:
        if fence.generation_task_id is not None and session.dirty:
            actual_status = session.connection().scalar(select(GenerationTask.status).where(
                GenerationTask.id == fence.generation_task_id,
            ).with_for_update())
            if actual_status is None or actual_status == "cancelled":
                raise StaleChapterWrite("生成任务已取消或删除，已停止写入")
        for task in [row for row in session.dirty if isinstance(row, GenerationTask) and session.is_modified(row)]:
            actual_status = session.connection().scalar(select(GenerationTask.status).where(
                GenerationTask.id == task.id,
            ).with_for_update())
            if actual_status == "cancelled":
                raise StaleChapterWrite("生成任务已取消，已停止写入")
    chapters = [row for row in session.dirty if isinstance(row, Chapter) and session.is_modified(row)]
    if not chapters:
        return
    connection = session.connection()
    if fence.pipeline_id is not None:
        owner = connection.execute(select(PipelineRun.command_id, PipelineRun.worker_id, PipelineRun.state).where(
            PipelineRun.id == fence.pipeline_id,
        ).with_for_update()).first()
        if owner is None or owner.command_id != fence.command_id or owner.worker_id != fence.worker_id or owner.state not in ("generating", "pausing"):
            raise StaleChapterWrite("执行凭证已失效，已阻止旧任务覆盖正文")
    for chapter in chapters:
        if chapter.id != fence.chapter_id:
            raise StaleChapterWrite("任务尝试写入其他章节，已阻止")
        if fence.project_id is not None:
            project_id = connection.scalar(select(Volume.project_id).where(Volume.id == chapter.volume_id))
            if project_id != fence.project_id:
                raise StaleChapterWrite("章节不属于当前作品，已停止写入")
        if inspect(chapter).attrs.content_text.history.has_changes() and (
            not chapter.content_text or not chapter.content_text.strip()
        ):
            raise StaleChapterWrite("生成正文为空，已保留原稿")
        actual = connection.execute(select(Chapter.updated_at).where(
            Chapter.id == chapter.id,
        ).with_for_update()).first()
        expected = session.info.get(_PENDING, fence.expected_updated_at)
        if actual is None or actual.updated_at != expected:
            raise StaleChapterWrite("正文已被其他操作修改，已保留当前版本")
        # Hash update is atomic with the body write, even if the process dies
        # before it updates the pipeline task's completion projection.
        if fence.pipeline_id is not None:
            connection.execute(update(PipelineChapterStatus).where(
                PipelineChapterStatus.pipeline_id == fence.pipeline_id,
                PipelineChapterStatus.chapter_id == chapter.id,
            ).values(saved_hash=content_hash(chapter.content_text)))
        elif batch_owner is not None and fence.generation_task_id is not None:
            from app.services.batch_receipts import has_batch_save
            if (inspect(chapter).attrs.content_text.history.has_changes()
                    or has_batch_save(connection, fence.generation_task_id)):
                session.info[_BATCH_WRITING] = fence.generation_task_id
        elif task_owner is not None and fence.generation_task_id is not None:
            from app.services.generation_task_receipts import has_task_save
            if (inspect(chapter).attrs.content_text.history.has_changes()
                    or has_task_save(connection, fence.generation_task_id)):
                session.info[_TASK_WRITING] = fence.generation_task_id
        session.info[_WRITING] = chapter
        fence.touched_sessions.add(session)


def _after_flush(session, flush_context):
    chapter = session.info.pop(_WRITING, None)
    if chapter is not None:
        session.info[_PENDING] = chapter.updated_at
        fence = _fence.get()
        if fence.pipeline_id is not None:
            session.connection().execute(update(PipelineChapterStatus).where(
                PipelineChapterStatus.pipeline_id == fence.pipeline_id,
                PipelineChapterStatus.chapter_id == chapter.id,
            ).values(saved_updated_at=chapter.updated_at))
        task_id = session.info.pop(_BATCH_WRITING, None)
        if task_id is not None:
            from app.services.batch_receipts import record_batch_save
            record_batch_save(session.connection(), chapter, task_id)
        independent_id = session.info.pop(_TASK_WRITING, None)
        if independent_id is not None:
            from app.services.generation_task_receipts import record_task_save
            record_task_save(session.connection(), chapter, independent_id)


def _after_commit(session):
    fence = _fence.get()
    if fence is not None and _PENDING in session.info:
        fence.expected_updated_at = session.info.pop(_PENDING)


def _after_rollback(session):
    session.info.pop(_PENDING, None)
    session.info.pop(_WRITING, None)
    session.info.pop(_BATCH_WRITING, None)
    session.info.pop(_TASK_WRITING, None)


event.listen(Session, "before_flush", _before_flush)
event.listen(Session, "after_flush_postexec", _after_flush)
event.listen(Session, "after_commit", _after_commit)
event.listen(Session, "after_rollback", _after_rollback)
