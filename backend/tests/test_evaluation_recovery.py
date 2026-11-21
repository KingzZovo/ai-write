import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import evaluation_recovery as recovery


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(recovery, 'claim_recovery_batch', AsyncMock(side_effect=lambda *a, **k: [row.id]))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(recovery, 'utcnow', lambda: now)
    row = NS(id=uuid4(), chapter_id=uuid4(), status='pending', updated_at=now-timedelta(minutes=3), created_at=now-timedelta(minutes=4))
    state = NS(locked=False, commits=0, busy=False)
    async def commit(): state.commits += 1
    db = NS(scalars=AsyncMock(return_value=NS(all=lambda: [row.id])), get=AsyncMock(return_value=row), commit=AsyncMock(side_effect=commit))
    @asynccontextmanager
    async def sessions(): yield db
    lease = NS(check=AsyncMock(), session=sessions)
    @asynccontextmanager
    async def lock(task_id):
        if state.busy:
            yield False
            return
        state.locked = True
        try: yield lease
        finally: state.locked = False
    monkeypatch.setattr(recovery, 'evaluation_worker_lock', lock)
    async def dispatch(tid, cid):
        assert not state.locked and state.commits == 1
        assert tid == str(row.id) and cid == str(row.chapter_id)
        return True
    return NS(row=row, state=state, db=db, sessions=sessions, dispatch=AsyncMock(side_effect=dispatch), now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'running'])
async def test_stale_task_requeues_same_id_after_commit_and_unlock(env, status):
    env.row.status = status
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['enqueued'] == 1 and result['failed'] == 0
    assert env.row.status == status and env.row.updated_at == env.now
    env.dispatch.assert_awaited_once()
    assert (await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch))['skipped'] == 1
    env.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_lock_owner_is_never_reset_or_requeued(env):
    env.state.busy = True
    before = env.row.updated_at
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['busy'] == 1 and env.row.updated_at == before
    env.db.get.assert_not_awaited()
    env.dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'failed'])
async def test_status_changed_since_scan_is_not_restarted(env, status):
    env.row.status = status
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['skipped'] == 1
    env.dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [False, True])
async def test_broker_failure_retains_task_and_reserves_retry_window(env, error):
    env.dispatch = AsyncMock(return_value=False, side_effect=RuntimeError('broker') if error else None)
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['failed'] == 1 and result['enqueued'] == 0
    assert env.row.status == 'pending' and env.row.updated_at == env.now


@pytest.mark.asyncio
async def test_external_cancellation_propagates_with_durable_task_retained(env):
    env.dispatch = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert env.row.status == 'pending' and not env.state.locked


def test_periodic_recovery_uses_async_bridge_and_expiring_messages(monkeypatch):
    from app import tasks
    from app.tasks import evaluation_tasks
    monkeypatch.setattr(tasks, '_run_async_safe', asyncio.run)
    call = AsyncMock(return_value={'examined': 0})
    monkeypatch.setattr(recovery, 'recover_evaluation_tasks', call)
    assert evaluation_tasks.recover_pending_evaluations.run() == {'examined': 0}
    call.assert_awaited_once()
    config = tasks.celery_app.conf.beat_schedule['evaluation-recovery']
    assert config['task'] == 'evaluations.recover_pending'
    assert config['options']['expires'] == config['schedule'] == 60


@pytest.mark.asyncio
async def test_commit_failure_never_publishes(env):
    env.db.commit.side_effect = RuntimeError('commit failed')
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['failed'] == 1
    assert not env.state.locked
    env.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_task_is_skipped(env):
    env.db.get.return_value = None
    result = await recovery.recover_evaluation_tasks(env.sessions, dispatch=env.dispatch)
    assert result['skipped'] == 1
    env.db.commit.assert_not_awaited()
    env.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_dispatch_uses_original_id_and_recovery_caller(monkeypatch):
    from app.tasks import evaluation_tasks
    from unittest.mock import Mock
    publish = Mock(return_value=True)
    monkeypatch.setattr(evaluation_tasks, 'dispatch_evaluate_task', publish)
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(recovery.asyncio, 'to_thread', to_thread)
    assert await recovery._dispatch('task-id', 'chapter-id') is True
    publish.assert_called_once_with('task-id', 'chapter-id', caller='evaluation.recovery')
