"""Shared project exclusion and version protection for standalone prose writers."""
from __future__ import annotations

import asyncio
from contextlib import aclosing, nullcontext
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services.production.control import ACTIVE_STATES, PipelineConflict
from app.services.production.fencing import StaleChapterWrite, standalone_write_fence
from app.services.production.locking import project_worker_lock


async def guarded_stream(engine, sessions, project_id, source, *, chapter_id=None,
                         volume_id=None, chapter_idx=None, expected_updated_at=None,
                         heartbeat_seconds=15, generation_task_id=None, metadata_only=False,
                         expected_chapter_source=None):
    """Hold the project lock until source and any in-flight __anext__ are drained.

    `source` is called only after ownership/version checks. The fence context
    propagates to child tasks and fresh sessions, including salvage saves.
    """
    project_id = UUID(str(project_id))
    async with project_worker_lock(engine, project_id) as lease:
        if not lease:
            raise PipelineConflict("本书已有生成任务正在写入，请稍后重试")
        async with sessions() as db:
            project = await db.get(Project, project_id)
            if project is None or project.deleted_at is not None:
                raise ValueError("作品不存在或已删除")
            active = await db.scalar(select(PipelineRun.id).where(
                PipelineRun.project_id == project_id, PipelineRun.state.in_(ACTIVE_STATES),
            ).limit(1))
            if active is not None:
                raise PipelineConflict("本书已有整书任务，请先暂停后再生成单章")
            fence = nullcontext()
            if not metadata_only:
                query = select(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == project_id)
                if chapter_id:
                    query = query.where(Chapter.id == UUID(str(chapter_id)))
                elif volume_id is not None and chapter_idx is not None:
                    query = query.where(Chapter.volume_id == UUID(str(volume_id)), Chapter.chapter_idx == chapter_idx)
                else:
                    raise ValueError("请选择要生成的章节")
                chapter = (await db.scalars(query)).one_or_none()
                if chapter is None:
                    raise ValueError("章节不存在或不属于当前作品")
                if expected_updated_at is not None and expected_updated_at != chapter.updated_at:
                    raise StaleChapterWrite("章节在准备期间已更新，请重新生成")
                if expected_chapter_source is not None:
                    from app.services.generation_submissions import chapter_submission_source
                    if chapter_submission_source(chapter) != expected_chapter_source:
                        raise StaleChapterWrite("章节在任务提交后已变化，请核对保存稿后重新提交；未开始生成")
                target_id, version = chapter.id, chapter.updated_at
                fence = standalone_write_fence(project_id, target_id, version, generation_task_id)
        with fence:
            async with aclosing(source()) as stream:
                pending = None
                checked = monotonic()
                try:
                    while True:
                        pending = asyncio.create_task(anext(stream))
                        while True:
                            done, _ = await asyncio.wait((pending,), timeout=heartbeat_seconds)
                            if monotonic() - checked >= heartbeat_seconds:
                                await lease.check()
                                checked = monotonic()
                            if done:
                                break
                        try:
                            item = pending.result()
                        except StopAsyncIteration:
                            return
                        yield item
                finally:
                    if pending is not None:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)


async def guarded_call(engine, sessions, project_id, operation, **target):
    async def source():
        yield await operation()
    async with aclosing(guarded_stream(engine, sessions, project_id, source, **target)) as stream:
        return await anext(stream)


async def guarded_project_call(engine, sessions, project_id, operation):
    """Serialize metadata regeneration; prose callers must use guarded_call."""
    return await guarded_call(engine, sessions, project_id, operation, metadata_only=True)
