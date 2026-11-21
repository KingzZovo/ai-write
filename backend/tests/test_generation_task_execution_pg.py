"""PG ownership/concurrency cases, only for an explicit disposable test schema.

Offline runs skip this file. No fixture probes or application connections are
permitted as a fallback when the dedicated test database is unavailable.
"""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update

from app.api import generate as generation_api
from app.db import session
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline, Volume
from app.services import generation_task_execution as ownership
from app.services.generation_execution import GenerationExecutionSession, GenerationExecutionUnavailable
from app.services.generation_submissions import chapter_submission_source
from app.services.manuscript_receipts import manuscript_source
from app.services.production.control import PipelineConflict, content_hash, request_start
from app.services.production.locking import try_command_lock
from app.services.production.fencing import StaleChapterWrite
from app.services.volume_outline_writes import volume_submission_source
from app.tasks import generation_tasks
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services
DELIVERED = '弟弟把两千元收进抽屉，剩下的五百元留在信封里。'


async def submit(r, monkeypatch, task_type='chapter'):
    monkeypatch.setattr(session, 'async_session_factory', r.sessions)
    monkeypatch.setattr(session._state, 'engine', r.engine)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        volume = await db.get(Volume, chapter.volume_id)
        params = {'chapter_id': str(chapter.id), 'volume_idx': volume.volume_idx,
            'submission': {'version': 1, 'fingerprint': 'e' * 64}}
        if task_type == 'chapter': params['submission']['chapter_source'] = chapter_submission_source(chapter)
        if task_type == 'outline_volume': params['submission']['volume_source'] = volume_submission_source(volume)
        identity = uuid4()
        db.add(GenerationTask(id=identity, project_id=r.project, task_type=task_type, params_json=params))
        await db.commit()
        return identity


async def run(identity):
    return await generation_tasks._run_guarded_generation(str(identity))


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'])
async def test_pg_all_independent_types_exclude_other_generators_and_book_commands(runtime_db, monkeypatch, task_type):
    r = runtime_db
    identity = await submit(r, monkeypatch, task_type)
    other_id = await submit(r, monkeypatch, 'outline_book')
    entered, release = asyncio.Event(), asyncio.Event()
    calls, pids = [], []
    async def generate(task_id, *, session_factory, read_session_factory):
        calls.append(task_id)
        async with session_factory() as db:
            pids.append(await db.scalar(text('SELECT pg_backend_pid()')))
            await db.commit()
        entered.set()
        await release.wait()
        # A fresh identity map must still use the same lock-owning backend.
        async with session_factory() as db:
            pids.append(await db.scalar(text('SELECT pg_backend_pid()')))
            task = await db.get(GenerationTask, task_id)
            task.status, task.result_text = 'completed', DELIVERED
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', generate)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await run(identity)
        with pytest.raises(PipelineConflict): await run(other_id)
        async with r.sessions() as db:
            with pytest.raises(PipelineConflict): await request_start(db, r.pipeline)
            await db.rollback()
            row = await db.get(GenerationTask, identity)
            assert row.execution_id is not None and row.attempt_count == 1
        release.set()
        await pending
        assert len(calls) == 1 and len(pids) == 2 and pids[0] == pids[1]
        async with r.sessions() as db:
            assert await try_command_lock(db, r.project)
            await db.rollback()
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pg_body_and_receipt_are_invisible_until_the_same_commit_and_rollback_together(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    async def generate(task_id, *, session_factory, read_session_factory):
        for commit in (False, True):
            async with session_factory() as db:
                chapter = await db.get(Chapter, r.chapter)
                chapter.content_text, chapter.status, chapter.word_count = DELIVERED, 'completed', len(DELIVERED)
                await db.flush()
                async with read_session_factory() as observer:
                    assert (await observer.get(Chapter, r.chapter)).content_text != DELIVERED
                    assert (await observer.get(GenerationTask, identity)).saved_manuscript_json is None
                if commit:
                    await db.commit()
                else:
                    await db.rollback()
        async with session_factory() as db:
            task = await db.get(GenerationTask, identity)
            task.status, task.result_text = 'completed', DELIVERED
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', generate)
    await run(identity)
    async with r.sessions() as db:
        chapter, task = await db.get(Chapter, r.chapter), await db.get(GenerationTask, identity)
        assert chapter.content_text == DELIVERED
        assert task.saved_manuscript_json['body_hash'] == content_hash(DELIVERED)
        assert task.saved_manuscript_json['source'] == manuscript_source(chapter)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_write', ['manuscript', 'outline', 'failure'])
async def test_pg_terminated_connection_cannot_reconnect_for_any_late_write(runtime_db, monkeypatch, late_write):
    r = runtime_db
    identity = await submit(r, monkeypatch, 'chapter' if late_write == 'manuscript' else 'outline_book')
    entered, release = asyncio.Event(), asyncio.Event()
    actor = {}
    async def generate(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            actor['pid'] = await db.scalar(text('SELECT pg_backend_pid()'))
            await db.commit()
        entered.set()
        await release.wait()
        async with session_factory() as db:
            if late_write == 'manuscript':
                chapter = await db.get(Chapter, r.chapter)
                chapter.content_text = DELIVERED
            elif late_write == 'outline':
                db.add(Outline(project_id=r.project, level='book', content_json={'raw_text': '迟到书纲。'}))
            else:
                await db.execute(update(GenerationTask).where(GenerationTask.id == identity).values(status='failed'))
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', generate)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with r.sessions() as author:
            assert actor['pid'] != await author.scalar(text('SELECT pg_backend_pid()'))
            # Only the process opened by this test in its disposable schema.
            assert await author.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': actor['pid']})
            await author.commit()
            async with asyncio.timeout(5):
                while not await try_command_lock(author, r.project):
                    await author.rollback()
                    await asyncio.sleep(.01)
            chapter = await author.get(Chapter, r.chapter)
            chapter.content_text = '作者已保存的新稿。'
            task = await author.get(GenerationTask, identity)
            task.status, task.result_text = 'needs_review', '作者保留的候选。'
            await author.commit()
        release.set()
        with pytest.raises(GenerationExecutionUnavailable): await pending
        async with r.sessions() as db:
            assert (await db.get(Chapter, r.chapter)).content_text == '作者已保存的新稿。'
            task = await db.get(GenerationTask, identity)
            assert task.status == 'needs_review' and task.result_text == '作者保留的候选。'
            assert task.saved_manuscript_json is None
            assert (await db.scalars(select(Outline).where(Outline.project_id == r.project))).all() == []
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pg_heartbeat_observes_cancellation_and_releases_the_owned_connection(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    entered, drained = asyncio.Event(), asyncio.Event()
    async def generate(*a, **k):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', generate)
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', .01)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with r.sessions() as db:
            task = await db.get(GenerationTask, identity, with_for_update=True)
            task.status = 'cancelled'
            task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
            await db.commit()
        with pytest.raises(ownership.TaskExecutionLost): await asyncio.wait_for(pending, 5)
        assert drained.is_set()
        async with r.sessions() as db:
            assert await try_command_lock(db, r.project)
            await db.rollback()
            task = await db.get(GenerationTask, identity)
            assert task.status == 'cancelled' and task.saved_manuscript_json is None
    finally:
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pg_claim_rejection_cannot_overwrite_an_accepted_http_cancellation(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    async with r.sessions() as db:
        await db.execute(update(GenerationTask).where(GenerationTask.id == identity).values(params_json={}))
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = GenerationExecutionSession.get
    async def hold_rejection_read(db, model, key, **kwargs):
        row = await original(db, model, key, **kwargs)
        if model is GenerationTask and not entered.is_set():
            entered.set()
            await release.wait()
        return row
    monkeypatch.setattr(GenerationExecutionSession, 'get', hold_rejection_read)
    async def cancel():
        async with r.sessions() as db:
            try:
                return await generation_api.cancel_async_generation(str(identity), db=db)
            except HTTPException as exc:
                return exc.status_code
    pending, cancelling = asyncio.create_task(run(identity)), None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancelling = asyncio.create_task(cancel())
        # A locking rejection keeps the cancel request waiting; an unlocked
        # stale read permits the accepted cancellation that must be preserved.
        await asyncio.wait({cancelling}, timeout=.2)
        release.set()
        with pytest.raises(StaleChapterWrite): await pending
        result = await asyncio.wait_for(cancelling, 5)
        async with r.sessions() as db:
            task = await db.get(GenerationTask, identity)
            if isinstance(result, dict):
                assert result['status'] == task.status == 'cancelled'
                assert task.params_json['cancelled_at']
            else:
                assert result == 409 and task.status == 'failed'
            assert task.attempt_count == 0 and task.execution_id is None
    finally:
        release.set()
        for child in (pending, cancelling):
            if child is not None and not child.done(): child.cancel()
        await asyncio.gather(*(child for child in (pending, cancelling) if child is not None), return_exceptions=True)
