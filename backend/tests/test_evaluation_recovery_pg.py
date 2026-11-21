"""Real PostgreSQL recovery reservation and worker mutex integration gate."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from tests.test_pipeline_control import runtime_db
from app.models.project import EvaluateTask
from app.services.evaluation_lock import evaluation_worker_lock
from app.services.evaluation_recovery import recover_evaluation_tasks


@pytest.mark.asyncio
async def test_live_worker_excluded_then_original_task_requeued_once(runtime_db, monkeypatch):
    r = runtime_db
    from app.services import evaluation_recovery
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(evaluation_recovery, 'utcnow', lambda: now)
    from app.db import session
    monkeypatch.setattr(session, 'engine', r.engine)
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c))
    async with r.sessions() as db:
        task = EvaluateTask(chapter_id=r.chapter, status='running',
                            created_at=now - timedelta(minutes=3),
                            updated_at=datetime.now(timezone.utc) - timedelta(minutes=3))
        db.add(task)
        await db.commit()
        tid = task.id
    publish = AsyncMock(return_value=True)
    async with evaluation_worker_lock(tid) as lease:
        assert lease
        result = await recover_evaluation_tasks(r.sessions, dispatch=publish)
        assert result['busy'] == 1
        publish.assert_not_awaited()
    now += timedelta(seconds=61)
    result = await recover_evaluation_tasks(r.sessions, dispatch=publish)
    assert result['enqueued'] == 1
    publish.assert_awaited_once_with(str(tid), str(r.chapter))
    assert (await recover_evaluation_tasks(r.sessions, dispatch=publish))['examined'] == 0
    async with r.sessions() as db:
        stored = await db.get(EvaluateTask, tid)
        assert stored.status == 'running'


@pytest.mark.asyncio
async def test_busy_oldest_task_rotates_and_does_not_hide_stranded_task(runtime_db, monkeypatch):
    r = runtime_db
    from app.db import session
    monkeypatch.setattr(session, 'engine', r.engine)
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c))
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with r.sessions() as db:
        busy = EvaluateTask(chapter_id=r.chapter, status='running', created_at=old, updated_at=old)
        stranded = EvaluateTask(chapter_id=r.chapter, status='running',
                                created_at=old + timedelta(minutes=1), updated_at=old)
        db.add_all([busy, stranded])
        await db.commit()
        busy_id, stranded_id = busy.id, stranded.id
    publish = AsyncMock(return_value=True)
    async with evaluation_worker_lock(busy_id) as lease:
        assert lease
        assert (await recover_evaluation_tasks(r.sessions, dispatch=publish, limit=1))['busy'] == 1
        assert (await recover_evaluation_tasks(r.sessions, dispatch=publish, limit=1))['enqueued'] == 1
    publish.assert_awaited_once_with(str(stranded_id), str(r.chapter))
    async with r.sessions() as db:
        stored = await db.get(EvaluateTask, busy_id)
        assert stored.updated_at == old and stored.status == 'running'
        assert stored.recovery_checked_at > old


@pytest.mark.asyncio
async def test_concurrent_scan_claims_do_not_select_same_task(runtime_db):
    import asyncio
    from app.services.evaluation_recovery import claim_recovery_batch
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c))
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with r.sessions() as db:
        tasks = [EvaluateTask(chapter_id=r.chapter, status='pending', created_at=old, updated_at=old)
                 for _ in range(2)]
        db.add_all(tasks)
        await db.commit()
        expected = {task.id for task in tasks}
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    first, second = await asyncio.gather(
        claim_recovery_batch(r.sessions, cutoff=cutoff, limit=1),
        claim_recovery_batch(r.sessions, cutoff=cutoff, limit=1),
    )
    assert len(first) == len(second) == 1
    assert set(first).isdisjoint(second) and set(first + second) == expected
