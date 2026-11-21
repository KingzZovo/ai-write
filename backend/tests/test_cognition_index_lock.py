"""No runtime connections: guard lifecycle and actual index-call boundaries."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import cognition_retrieval as retrieval
from app.services.cognition_index_lock import cognition_index_key, cognition_index_lock
from app.services.cognition_index_maintenance import cleanup_project_indexes
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_materialization_lock import materialization_key
from app.services.evaluation_lock import evaluation_lock_key
from app.services.production.locking import project_lock_key
from tests.test_cognition_retrieval import FixedSpace, prompt, sessions_for, vector_db


def test_index_lock_has_its_own_stable_namespace():
    pid = uuid4()
    assert cognition_index_key(pid) == cognition_index_key(str(pid))
    assert len({cognition_index_key(pid), project_lock_key(pid), evaluation_lock_key(pid), materialization_key(pid)}) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['waiting', 'busy', 'acquire_error', 'commit_error', 'cancel'])
async def test_physical_lock_connection_is_discarded_on_every_outcome(monkeypatch, outcome):
    from app.db import session
    connection = NS(scalar=AsyncMock(return_value=False), commit=AsyncMock(), invalidate=AsyncMock(),
        execute=AsyncMock(), invalidated=False, closed=False)
    if outcome == 'acquire_error':
        connection.scalar.side_effect = RuntimeError('lost acquisition reply')
    if outcome == 'commit_error':
        connection.commit.side_effect = RuntimeError('lost commit reply')
    @asynccontextmanager
    async def connect():
        yield connection
    monkeypatch.setattr(session, 'engine', NS(connect=connect))
    async def run():
        async with cognition_index_lock(uuid4(), wait=outcome != 'busy') as lease:
            assert bool(lease) == (outcome != 'busy')
            connection.commit.assert_awaited_once()
            if outcome == 'cancel':
                raise asyncio.CancelledError()
            if outcome != 'busy':
                await lease.check()
    if outcome in {'acquire_error', 'commit_error'}:
        with pytest.raises(RuntimeError):
            await run()
    elif outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await run()
    else:
        await run()
    connection.invalidate.assert_awaited_once()
    statement = str(connection.scalar.await_args.args[0])
    assert ('pg_try_advisory_lock' in statement) == (outcome == 'busy')


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_at', [1, 2, 3])
async def test_lost_index_guard_cannot_return_usable_context(vector_db, monkeypatch, lost_at):
    request = prompt()
    space = FixedSpace()
    check = AsyncMock(side_effect=[None] * (lost_at - 1) + [RuntimeError('connection lost')])
    @asynccontextmanager
    async def lock(pid, *, wait):
        assert pid == request.project_id and wait
        yield NS(check=check)
    with pytest.raises(RequiredContextUnavailable):
        await retrieval.resolve_prompt_context(request, client=vector_db,
            sessions=sessions_for(request.ledger, monkeypatch), space_loader=AsyncMock(return_value=space),
            lock_factory=lock)
    if lost_at == 1:
        assert not space.calls


@pytest.mark.asyncio
async def test_same_book_prepares_once_and_cleanup_skips_in_use_index(vector_db, monkeypatch):
    request = prompt()
    mutex = asyncio.Lock()
    entered = asyncio.Event()
    release = asyncio.Event()
    @asynccontextmanager
    async def lock(pid, *, wait=False):
        if not wait and mutex.locked():
            yield False
            return
        async with mutex:
            yield NS(check=AsyncMock())
    async def hook(kind, texts):
        if kind == 'passage' and not entered.is_set():
            entered.set()
            await release.wait()
    first_space, next_space = FixedSpace(hook), FixedSpace()
    sessions = sessions_for(request.ledger, monkeypatch)
    first = asyncio.create_task(retrieval.resolve_prompt_context(request, client=vector_db, sessions=sessions,
        space_loader=AsyncMock(return_value=first_space), lock_factory=lock))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(retrieval.resolve_prompt_context(request, client=vector_db, sessions=sessions,
            space_loader=AsyncMock(return_value=next_space), lock_factory=lock))
        await asyncio.sleep(0)
        assert not next_space.calls
        result = await cleanup_project_indexes(request.project_id, sessions, client=vector_db, lock_factory=lock)
        assert result['status'] == 'busy' and result['removed_points'] == 0
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []))
    assert results[0] == results[1]
    assert not any(kind == 'passage' for kind, _ in next_space.calls)
