"""Optional real-PG integration; offline mode must skip this whole module."""
import asyncio

import pytest

from app.services.cognition_index_lock import cognition_index_lock
from app.services.production.locking import project_worker_lock
from tests.test_pipeline_control import runtime_db

pytestmark = [pytest.mark.asyncio, pytest.mark.external_services]


async def test_guard_serializes_index_work_without_holding_transaction(runtime_db, monkeypatch):
    r = runtime_db
    monkeypatch.setattr('app.db.session.engine', r.engine)
    acquired = asyncio.Event()
    async def waiter():
        async with cognition_index_lock(r.project, wait=True) as lease:
            assert lease and not lease.connection.in_transaction()
            await lease.check()
            assert not lease.connection.in_transaction()
            acquired.set()
    second = None
    try:
        async with cognition_index_lock(r.project, wait=True) as first:
            assert not first.connection.in_transaction()
            async with cognition_index_lock(r.project, wait=False) as busy:
                assert not busy
            second = asyncio.create_task(waiter())
            await asyncio.sleep(0.05)
            assert not acquired.is_set()
        await asyncio.wait_for(second, 5)
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


async def test_index_guard_does_not_conflict_with_generation_project_lock(runtime_db, monkeypatch):
    r = runtime_db
    monkeypatch.setattr('app.db.session.engine', r.engine)
    async with project_worker_lock(r.engine, r.project) as generation:
        assert generation
        async with cognition_index_lock(r.project, wait=False) as index:
            assert index


async def test_cancelled_waiter_does_not_leave_an_advisory_lock(runtime_db, monkeypatch):
    r = runtime_db
    monkeypatch.setattr('app.db.session.engine', r.engine)
    async def waiter():
        async with cognition_index_lock(r.project, wait=True):
            pytest.fail('waiter acquired while the book was held')
    async with cognition_index_lock(r.project, wait=True):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with cognition_index_lock(r.project, wait=False) as next_owner:
        assert next_owner
