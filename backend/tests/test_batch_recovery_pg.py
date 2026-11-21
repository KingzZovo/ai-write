"""Real PG takeover/connection loss cases; never run against application data."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from app.db import session
from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Project
from app.services.batch_execution import StaleBatchExecution, batch_execution, batch_execution_lease
from app.services.batch_generator import BatchGenerator
from app.services.batch_receipts import receipt_matches
from app.services.batch_submissions import claim_batch, record_item
from app.services.production.locking import project_worker_lock
from app.tasks import generation_tasks
from tests.test_batch_submissions_pg import batch_book, submit
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services
DRAFT = '弟弟接过两千元，放好信封，又伸手扶住墙。'


@pytest_asyncio.fixture
async def pg_batch(batch_book, monkeypatch):
    r = batch_book
    monkeypatch.setattr(session, 'async_session_factory', r.sessions)
    monkeypatch.setattr(session._state, 'engine', r.engine)
    async def generate(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json['chapter_id'])
            chapter.content_text, chapter.word_count, chapter.status = DRAFT, len(DRAFT), 'completed'
            task.result_text, task.status = DRAFT, 'completed'
            await db.commit()
    r.model = AsyncMock(side_effect=generate)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', r.model)
    await submit(r)
    return r


def generator():
    result = BatchGenerator()
    result.hook_manager = NS(run_pre_hooks=AsyncMock(return_value=NS(can_proceed=True)),
        run_post_hooks=AsyncMock(), aclose=AsyncMock())
    return result


async def expire(r):
    async with r.sessions() as db:
        await db.execute(update(GenerationBatch).where(GenerationBatch.id == r.batch_id)
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=120)))
        await db.commit()


async def orphan(r):
    async with r.sessions() as db:
        async with batch_execution_lease(db, r.batch_id):
            wid = await claim_batch(db, r.project, r.batch_id)
            task_id = await db.scalar(select(GenerationBatchItem.task_id))
            await record_item(db, r.batch_id, wid, task_id, 'generating')
    await expire(r)
    return wid, task_id


async def recover(r):
    async with r.sessions() as db:
        return await generator().run_submitted(r.project, r.batch_id, db=db, recover=True)


@pytest.mark.asyncio
async def test_parallel_recovery_deliveries_share_one_actual_batch_lease(pg_batch):
    r = pg_batch
    await orphan(r)
    results = await asyncio.gather(*(recover(r) for _ in range(4)))
    assert any(job.status.value == 'completed' for job in results)
    r.model.assert_awaited_once()
    async with r.sessions() as db:
        item = await db.scalar(select(GenerationBatchItem))
        chapter = await db.get(Chapter, r.chapter)
        assert item.attempt_count == 2 and receipt_matches(item, chapter)


@pytest.mark.asyncio
@pytest.mark.parametrize('held', ['batch', 'project'])
async def test_expired_heartbeat_does_not_override_either_live_pg_mutex(pg_batch, held):
    r = pg_batch
    wid, _ = await orphan(r)
    async with r.sessions() as db:
        lock = batch_execution_lease(db, r.batch_id) if held == 'batch' else project_worker_lock(r.engine, r.project)
        async with lock as lease:
            assert lease
            assert (await recover(r)).status.value == 'running'
    async with r.sessions() as db:
        assert (await db.get(GenerationBatch, r.batch_id)).worker_id == wid
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_pg_saved_body_before_task_completion_becomes_review_without_new_model_work(pg_batch):
    r = pg_batch
    async def partial_commit(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json['chapter_id'])
            chapter.content_text, chapter.status = DRAFT, 'completed'
            await db.flush()
            async with r.sessions() as observer:
                assert (await observer.get(Chapter, r.chapter)).content_text != DRAFT
                assert (await observer.scalar(select(GenerationBatchItem))).saved_receipt_json is None
            await db.commit()
    r.model.side_effect = partial_commit
    async with r.sessions() as db:
        async with batch_execution_lease(db, r.batch_id):
            wid = await claim_batch(db, r.project, r.batch_id)
            task_id = await db.scalar(select(GenerationBatchItem.task_id))
            await record_item(db, r.batch_id, wid, task_id, 'generating')
            with batch_execution(r.batch_id, wid, task_id):
                await generation_tasks._run_guarded_generation(str(task_id))
    await expire(r)
    r.model.reset_mock()
    assert (await recover(r)).status.value == 'needs_review'
    r.model.assert_not_awaited()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == DRAFT


@pytest.mark.asyncio
@pytest.mark.parametrize('late_write', ['result', 'failure'])
async def test_terminated_old_batch_connection_cannot_write_after_real_takeover(pg_batch, late_write):
    r = pg_batch
    entered, release = asyncio.Event(), asyncio.Event()
    owner = {}
    async def old_actor():
        async with r.sessions() as db:
            async with batch_execution_lease(db, r.batch_id) as lease:
                wid = await claim_batch(db, r.project, r.batch_id)
                task_id = await db.scalar(select(GenerationBatchItem.task_id))
                await record_item(db, r.batch_id, wid, task_id, 'generating')
                owner['pid'] = await lease.connection.scalar(text('SELECT pg_backend_pid()'))
                await lease.connection.commit()
                with batch_execution(r.batch_id, wid, task_id):
                    entered.set()
                    await release.wait()
                    async with r.sessions() as writer:
                        if late_write == 'failure':
                            await writer.execute(update(GenerationTask).where(GenerationTask.id == task_id).values(status='failed'))
                        else:
                            task = await writer.get(GenerationTask, task_id)
                            task.result_text = '旧进程的迟到内容。'
                        await writer.commit()
    old = asyncio.create_task(old_actor())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await expire(r)
        async with r.sessions() as observer:
            assert owner['pid'] != await observer.scalar(text('SELECT pg_backend_pid()'))
            # Only the PID just created by this test in its disposable DB.
            assert await observer.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': owner['pid']})
            await observer.commit()
            async with asyncio.timeout(5):
                while await observer.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE pid=:pid'), {'pid': owner['pid']}):
                    await observer.rollback()
                    await asyncio.sleep(.01)
        assert (await recover(r)).status.value == 'completed'
        release.set()
        with pytest.raises(StaleBatchExecution): await old
        async with r.sessions() as observer:
            task = await observer.scalar(select(GenerationTask))
            assert task.status == 'completed' and task.result_text == DRAFT
    finally:
        release.set()
        if not old.done(): old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_post_hook_locks_project_before_batch_to_avoid_parent_child_deadlock(pg_batch):
    r = pg_batch
    wid, task_id = await orphan(r)
    entered = asyncio.Event()
    actor_pid = {}
    async def post_hook():
        async with r.sessions() as db:
            async with batch_execution_lease(db, r.batch_id):
                actor_pid['pid'] = await db.scalar(text('SELECT pg_backend_pid()'))
                await db.commit()
                with batch_execution(r.batch_id, wid, task_id):
                    entered.set()
                    await db.scalar(select(Project.id).where(Project.id == r.project).with_for_update(read=True))
                    await db.commit()
    async with r.sessions() as controller:
        await controller.scalar(select(Project.id).where(Project.id == r.project).with_for_update())
        actor = asyncio.create_task(post_hook())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with r.sessions() as observer:
                async with asyncio.timeout(5):
                    while not await observer.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE pid=:pid AND wait_event_type='Lock'"), actor_pid):
                        await observer.rollback()
                        await asyncio.sleep(.01)
            # A waiting post-hook must not already hold the child batch row.
            assert await controller.scalar(select(GenerationBatch.id).where(GenerationBatch.id == r.batch_id)
                .with_for_update(nowait=True)) == r.batch_id
        finally:
            await controller.rollback()
            await asyncio.wait_for(actor, 5)
