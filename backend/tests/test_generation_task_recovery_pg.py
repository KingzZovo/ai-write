"""Recovery on actual PG leases; offline collection must skip all fixtures."""
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import event, select, text, update

from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline
from app.services import generation_task_execution as ownership
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.generation_task_dispatch import dispatch_tasks
from app.services.production.control import PipelineConflict
from app.tasks import generation_tasks
from tests.test_generation_task_execution_pg import DELIVERED, run, submit
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


async def expire(r, identity):
    async with r.sessions() as db:
        await db.execute(update(GenerationTask).where(GenerationTask.id == identity)
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=120)))
        await db.commit()


async def recover(identity):
    return await generation_tasks._run_guarded_generation(str(identity), recover=True)


@pytest.mark.asyncio
async def test_pg_old_heartbeat_cannot_take_over_a_still_live_project_connection(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', 3600)
    async def hold(task_id, *, session_factory, read_session_factory):
        entered.set()
        await release.wait()
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            task.status, task.result_text = 'completed', DELIVERED
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', hold)
    first = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await expire(r, identity)
        with pytest.raises(PipelineConflict): await recover(identity)
        async with r.sessions() as db:
            task = await db.get(GenerationTask, identity)
            assert task.attempt_count == 1 and task.recovery_history_json == []
        release.set()
        await first
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_write', ['manuscript', 'outline', 'failure'])
async def test_pg_killed_old_connection_cannot_save_after_real_recovery(runtime_db, monkeypatch, late_write):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls, actor = [], {}
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', 3600)
    async def operation(task_id, *, session_factory, read_session_factory):
        calls.append(task_id)
        if len(calls) == 1:
            async with session_factory() as db:
                actor['pid'] = await db.scalar(text('SELECT pg_backend_pid()'))
                await db.commit()
            entered.set()
            await release.wait()
            async with session_factory() as db:
                if late_write == 'manuscript':
                    (await db.get(Chapter, r.chapter)).content_text = '旧执行的迟到正文。'
                elif late_write == 'outline':
                    db.add(Outline(project_id=r.project, level='book', content_json={'raw_text': '旧书纲。'}))
                else:
                    await db.execute(update(GenerationTask).where(GenerationTask.id == identity).values(status='failed'))
                await db.commit()
        else:
            async with session_factory() as db:
                chapter = await db.get(Chapter, r.chapter)
                chapter.content_text, chapter.status, chapter.word_count = DELIVERED, 'completed', len(DELIVERED)
                task = await db.get(GenerationTask, identity)
                task.status, task.result_text = 'completed', DELIVERED
                await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    old = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await expire(r, identity)
        async with r.sessions() as db:
            original_id = (await db.get(GenerationTask, identity)).execution_id
            assert actor['pid'] != await db.scalar(text('SELECT pg_backend_pid()'))
            assert await db.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': actor['pid']})
            await db.commit()
            async with asyncio.timeout(5):
                while await db.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE pid=:pid'), {'pid': actor['pid']}):
                    await db.rollback()
                    await asyncio.sleep(.01)
        await recover(identity)
        release.set()
        with pytest.raises((GenerationExecutionUnavailable, ownership.TaskExecutionLost)): await old
        async with r.sessions() as db:
            task = await db.get(GenerationTask, identity)
            assert task.attempt_count == 2 and task.execution_id != original_id
            assert task.status == 'completed' and task.result_text == DELIVERED
            assert (await db.get(Chapter, r.chapter)).content_text == DELIVERED
            assert task.saved_manuscript_json['execution_id'] == str(task.execution_id)
            assert len(task.recovery_history_json) == 1 and len(calls) == 2
    finally:
        release.set()
        if not old.done(): old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_pg_saved_body_and_receipt_are_retained_without_a_second_generation(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    calls = []
    async def save_then_exit(task_id, *, session_factory, read_session_factory):
        calls.append(task_id)
        async with session_factory() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text, chapter.status, chapter.word_count = DELIVERED, 'completed', len(DELIVERED)
            await db.commit()
        raise asyncio.CancelledError()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', save_then_exit)
    with pytest.raises(asyncio.CancelledError): await run(identity)
    await expire(r, identity)
    async with r.sessions() as db: proof = (await db.get(GenerationTask, identity)).saved_manuscript_json
    await recover(identity)
    async with r.sessions() as db:
        task = await db.get(GenerationTask, identity)
        assert task.status == 'needs_review' and task.saved_manuscript_json == proof
        assert task.attempt_count == 1 and calls == [str(identity)]
        assert (await db.get(Chapter, r.chapter)).content_text == DELIVERED


@pytest.mark.asyncio
async def test_pg_dispatch_skips_a_locked_task_without_starving_later_ids(runtime_db, monkeypatch):
    r = runtime_db
    identities = [await submit(r, monkeypatch) for _ in range(3)]
    delivered = []
    async def publish(key): delivered.append(key)
    async with r.sessions() as locked:
        await locked.scalar(select(GenerationTask.id).where(GenerationTask.id == identities[0]).with_for_update())
        assert await asyncio.wait_for(dispatch_tasks(r.sessions, publish, limit=2), 5) == 2
        assert set(delivered) == {str(key) for key in identities[1:]}
        await locked.rollback()
    assert await dispatch_tasks(r.sessions, publish, limit=1) == 1
    assert delivered[-1] == str(identities[0])


@pytest.mark.asyncio
async def test_pg_downgrade_waits_for_a_concurrent_source_write_and_preserves_it(runtime_db, monkeypatch):
    r = runtime_db
    identity = await submit(r, monkeypatch)
    path = Path(__file__).parents[1] / 'alembic/versions/a1001956_generation_task_recovery.py'
    spec = importlib.util.spec_from_file_location('task_recovery_pg_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    entered = asyncio.Event()
    def observe(_c, _u, sql, _p, _ctx, _m):
        if sql.startswith('LOCK TABLE generation_tasks IN ACCESS EXCLUSIVE'): entered.set()
    event.listen(r.engine.sync_engine, 'before_cursor_execute', observe)
    async def downgrade():
        async with r.engine.begin() as connection:
            def apply(sync):
                migration.op = Operations(MigrationContext.configure(sync))
                migration.downgrade()
            await connection.run_sync(apply)
    pending = None
    try:
        async with r.sessions() as author:
            task = await author.get(GenerationTask, identity)
            task.execution_source_json = {'version': 1, 'material_hash': 'c' * 64, 'task_type': 'chapter'}
            await author.flush()
            pending = asyncio.create_task(downgrade())
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(TimeoutError): await asyncio.wait_for(asyncio.shield(pending), .05)
            await author.commit()
        with pytest.raises(RuntimeError, match='恢复来源或派发历史'): await pending
        async with r.sessions() as db:
            assert (await db.get(GenerationTask, identity)).execution_source_json['material_hash'] == 'c' * 64
    finally:
        if pending is not None:
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(r.engine.sync_engine, 'before_cursor_execute', observe)
