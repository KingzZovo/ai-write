"""Shared lock, version and cancellation invariants for standalone writers."""
import asyncio
import contextvars
from contextlib import aclosing
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from app.models.generation_task import GenerationTask
from app.models.project import Chapter
from app.services.production.control import PipelineConflict, request_start
from app.services.production.fencing import StaleChapterWrite
from app.services.production.writer import guarded_call, guarded_stream


async def save(r, text):
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = text
        await db.commit()


@pytest.mark.asyncio
async def test_standalone_blocks_pipeline_and_other_writer(runtime_db):
    r = runtime_db
    async def first():
        async with r.sessions() as db:
            with pytest.raises(PipelineConflict):
                await request_start(db, r.pipeline)
        with pytest.raises(PipelineConflict):
            await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, "竞争稿。"), chapter_id=r.chapter)
        await save(r, "完整正文。")
    await guarded_call(r.engine, r.sessions, r.project, first, chapter_id=r.chapter)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "完整正文。"
        await request_start(db, r.pipeline)
        await db.commit()
    with pytest.raises(PipelineConflict):
        await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, "覆盖。"), chapter_id=r.chapter)


@pytest.mark.asyncio
async def test_manual_edit_is_preserved_in_fresh_session(runtime_db):
    r = runtime_db
    async def write():
        await contextvars.Context().run(asyncio.create_task, save(r, "作者修改。"))
        await save(r, "生成覆盖。")
    with pytest.raises(StaleChapterWrite):
        await guarded_call(r.engine, r.sessions, r.project, write, chapter_id=r.chapter)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "作者修改。"


@pytest.mark.asyncio
async def test_multiple_own_saves_advance_version(runtime_db):
    r = runtime_db
    async def write():
        await save(r, "初稿。")
        await save(r, "修订稿。")
    await guarded_call(r.engine, r.sessions, r.project, write, chapter_id=r.chapter)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "修订稿。"


@pytest.mark.asyncio
async def test_empty_and_foreign_chapter_rejected(runtime_db):
    r = runtime_db
    with pytest.raises(StaleChapterWrite):
        await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, " "), chapter_id=r.chapter)
    with pytest.raises(ValueError, match="不属于"):
        await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, "覆盖。"), chapter_id=uuid4())
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_stream_close_drains_source_and_releases_lock(runtime_db):
    r = runtime_db
    closed = asyncio.Event()
    async def source():
        try:
            yield "frame"
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with aclosing(guarded_stream(r.engine, r.sessions, r.project, source, chapter_id=r.chapter)) as stream:
        assert await anext(stream) == "frame"
    assert closed.is_set()
    await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, "后续正文。"), chapter_id=r.chapter)


@pytest.mark.asyncio
async def test_cancellation_drains_inflight_child(runtime_db):
    r = runtime_db
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def write():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    task = asyncio.create_task(guarded_call(r.engine, r.sessions, r.project, write, chapter_id=r.chapter))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    async with r.sessions() as db:
        await request_start(db, r.pipeline)
        await db.commit()


@pytest.mark.asyncio
async def test_cancelled_task_cannot_be_resaved_by_generator(runtime_db):
    r = runtime_db
    async with r.sessions() as db:
        task = GenerationTask(project_id=r.project, task_type="chapter", status="running")
        db.add(task)
        await db.commit()
        task_id = task.id
    async def cancel():
        async with r.sessions() as db:
            task = await db.get(GenerationTask, task_id)
            task.status = "cancelled"
            await db.commit()
    async def generate():
        async with r.sessions() as db:
            task = await db.get(GenerationTask, task_id)
            await contextvars.Context().run(asyncio.create_task, cancel())
            task.status = "completed"
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "不应保存。"
            await db.commit()
    with pytest.raises(StaleChapterWrite, match="取消"):
        await guarded_call(r.engine, r.sessions, r.project, generate, chapter_id=r.chapter)
    async with r.sessions() as db:
        assert (await db.get(GenerationTask, task_id)).status == "cancelled"
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_task_wrapper_obeys_shared_guard_and_skips_duplicate(runtime_db, monkeypatch):
    from app.db import session
    from app.tasks import generation_tasks
    r = runtime_db
    monkeypatch.setattr(session, "async_session_factory", r.sessions)
    monkeypatch.setattr(session._state, "engine", r.engine)
    async with r.sessions() as db:
        from app.services.generation_submissions import chapter_submission_source
        chapter = await db.get(Chapter, r.chapter)
        task = GenerationTask(project_id=r.project, task_type="chapter", params_json={
            "chapter_id": str(r.chapter), "submission": {
                "version": 1, "fingerprint": "a" * 64, "chapter_source": chapter_submission_source(chapter)}})
        db.add(task)
        await db.commit()
        task_id = str(task.id)
    calls = []
    async def generate(task_id, *, session_factory, read_session_factory=None):
        calls.append(task_id)
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            task.status = "completed"
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "共享保护的正文。"
            await db.commit()
    monkeypatch.setattr(generation_tasks, "_run_async_generation_impl", generate)
    await generation_tasks._run_guarded_generation(task_id)
    await generation_tasks._run_guarded_generation(task_id)
    assert calls == [task_id]


@pytest.mark.asyncio
async def test_cancellation_blocks_prose_only_salvage_session(runtime_db):
    r = runtime_db
    async with r.sessions() as db:
        task = GenerationTask(project_id=r.project, task_type="chapter", status="cancelled")
        db.add(task)
        await db.commit()
        task_id = task.id
    with pytest.raises(StaleChapterWrite, match="取消"):
        await guarded_call(r.engine, r.sessions, r.project, lambda: save(r, "晚到的正文。"),
                           chapter_id=r.chapter, generation_task_id=task_id)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_disconnect_waits_for_started_save_before_unlock(runtime_db):
    from app.api.generate import _await_chapter_save
    r = runtime_db
    entered, release = asyncio.Event(), asyncio.Event()
    async def persistence():
        entered.set()
        await release.wait()
        await save(r, "断开前已生成的正文。")
    async def operation():
        return await _await_chapter_save(asyncio.create_task(persistence()))
    writer = asyncio.create_task(guarded_call(r.engine, r.sessions, r.project, operation, chapter_id=r.chapter))
    await asyncio.wait_for(entered.wait(), 3)
    writer.cancel()
    try:
        async with r.sessions() as db:
            with pytest.raises(PipelineConflict):
                await request_start(db, r.pipeline)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "断开前已生成的正文。"
        await request_start(db, r.pipeline)
        await db.commit()


@pytest.mark.asyncio
async def test_lock_loss_stops_slow_standalone_writer(runtime_db, monkeypatch):
    from app.services.production.locking import WorkerLease
    r = runtime_db
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def lost(lease):
        await entered.wait()
        raise RuntimeError("lock lost")
    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(WorkerLease, "check", lost)
    with pytest.raises(RuntimeError, match="lock lost"):
        await guarded_call(r.engine, r.sessions, r.project, operation, chapter_id=r.chapter, heartbeat_seconds=0.01)
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_failed", [False, True])
async def test_async_api_commits_task_before_publish(runtime_db, monkeypatch, broker_failed):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from app.api import generate
    from app.tasks import knowledge_tasks
    r = runtime_db
    loop = asyncio.get_running_loop()
    monkeypatch.setattr("app.services.generation_submissions.validate_submission_outlines", AsyncMock())
    seen = []
    async def read(task_id):
        async with r.sessions() as db:
            task = await db.get(GenerationTask, task_id)
            assert task is not None and task.status == "pending"
            seen.append(task.id)
    def publish(*, args, task_id, retry):
        assert args == [task_id] and retry is False
        asyncio.run_coroutine_threadsafe(read(task_id), loop).result(timeout=3)
        if broker_failed:
            raise ConnectionError("unavailable")
        return SimpleNamespace(id="test-delivery")
    monkeypatch.setattr(knowledge_tasks.run_async_generation, "apply_async", publish)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        req = generate.AsyncGenerateRequest(request_id=str(uuid4()), project_id=str(r.project), chapter_id=str(r.chapter),
            task_type="chapter", expected_content_text=chapter.content_text or '')
        if broker_failed:
            with pytest.raises(HTTPException) as exc:
                await generate.start_async_generation(req, db)
            assert exc.value.status_code == 503
        else:
            await generate.start_async_generation(req, db)
            await db.commit()
    async with r.sessions() as db:
        task = await db.get(GenerationTask, seen[0])
        assert task.status == "pending"  # A broker exception cannot prove non-delivery.


@pytest.mark.asyncio
@pytest.mark.parametrize('independent_summary', [False, True])
async def test_derived_summary_does_not_invalidate_own_body_fence(runtime_db, monkeypatch, independent_summary):
    from unittest.mock import AsyncMock
    from app.services import chapter_summarizer as cs
    r = runtime_db
    body = '沈青记下六本台账的编号，把钥匙交给陈川，约定明早由他和站长核对。' * 10
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='钥匙已交陈川，正式核对留待明早。'))

    async def derive():
        async with r.sessions() as db:
            assert (await cs.summarize_and_save_chapter(
                chapter_id=r.chapter, db=db, project_vectors=False,
            ))[0]

    async def write():
        await save(r, body)
        async with r.sessions() as db:
            before = (await db.get(Chapter, r.chapter)).updated_at
        if independent_summary:
            await contextvars.Context().run(asyncio.create_task, derive())
        else:
            await derive()
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            assert chapter.updated_at == before
            chapter.status = 'completed'
            await db.commit()
    await guarded_call(r.engine, r.sessions, r.project, write, chapter_id=r.chapter)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        assert chapter.content_text == body
        assert chapter.status == 'completed'
        assert chapter.summary == '钥匙已交陈川，正式核对留待明早。'


@pytest.mark.asyncio
async def test_author_edit_after_derived_summary_still_fences_status_write(runtime_db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import chapter_summarizer as cs
    r = runtime_db
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='派生摘要。'))
    async def write():
        await save(r, '足够长的正文。' * 50)
        async with r.sessions() as db:
            assert (await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=db, project_vectors=False))[0]
        await contextvars.Context().run(asyncio.create_task, save(r, '作者亲自改定的正文。'))
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.status = 'completed'
            await db.commit()
    with pytest.raises(StaleChapterWrite):
        await guarded_call(r.engine, r.sessions, r.project, write, chapter_id=r.chapter)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        assert chapter.content_text == '作者亲自改定的正文。'
        assert chapter.status == 'draft'
