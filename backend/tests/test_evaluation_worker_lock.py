from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncio

import pytest

from app.services.evaluation_lock import evaluation_worker_lock, evaluation_lock_key
from app.services.production.locking import project_lock_key
from app.services.entity_materialization_lock import materialization_key


def test_evaluation_namespace_is_separate_and_stable():
    tid = uuid4()
    assert evaluation_lock_key(tid) == evaluation_lock_key(str(tid))
    assert len({evaluation_lock_key(tid), project_lock_key(tid), materialization_key(tid)}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ok', 'busy', 'cancel', 'commit_error'])
async def test_lock_connection_is_always_discarded_and_sessions_share_it(monkeypatch, outcome):
    from app.db import session
    from app.services import evaluation_lock
    connection = NS(scalar=AsyncMock(return_value=outcome != 'busy'), commit=AsyncMock(), invalidate=AsyncMock())
    if outcome == 'commit_error': connection.commit.side_effect = RuntimeError('commit')
    @asynccontextmanager
    async def connect(): yield connection
    monkeypatch.setattr(session, 'engine', NS(connect=connect))
    created = []
    monkeypatch.setattr(evaluation_lock, 'AsyncSession', lambda **kw: created.append(kw) or object())
    async def run():
        async with evaluation_worker_lock(uuid4()) as lease:
            assert bool(lease) == (outcome != 'busy')
            lease.session()
            assert created == [{'bind': connection, 'expire_on_commit': False}]
            connection.commit.assert_awaited_once()
            if outcome == 'cancel': raise asyncio.CancelledError()
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError): await run()
    elif outcome == 'commit_error':
        with pytest.raises(RuntimeError): await run()
    else: await run()
    connection.invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_delivery_does_not_load_or_evaluate(monkeypatch):
    from app.tasks import evaluation_tasks
    @asynccontextmanager
    async def busy(task_id): yield False
    monkeypatch.setattr('app.services.evaluation_lock.evaluation_worker_lock', busy)
    owned = AsyncMock()
    monkeypatch.setattr(evaluation_tasks, '_evaluate_task_owned', owned)
    result = await evaluation_tasks._run_evaluate_task_async('t', 'c', 'test')
    assert result['reason'] == 'already_running'
    owned.assert_not_awaited()
