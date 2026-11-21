"""Batch lease lifecycle using real SQLAlchemy connections and a fake PG transport."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.services import batch_execution as ownership


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'busy', 'acquire_error', 'commit_error',
    'cancel_acquire', 'cancel_body', 'body_error', 'non_postgres'])
async def test_batch_lease_discards_the_physical_connection_on_every_exit(tmp_path, monkeypatch, outcome):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "batch-lease.sqlite"}')
    record = NS(invalidated=0, exited=0)
    class Connection:
        dialect = NS(name='sqlite' if outcome == 'non_postgres' else 'postgresql')
        def __init__(self, sync): self.sync_connection = sync
        async def scalar(self, statement, params):
            record.key = params['key']
            if outcome == 'acquire_error': raise RuntimeError('test lock failure')
            if outcome == 'cancel_acquire': raise asyncio.CancelledError()
            return outcome != 'busy'
        async def commit(self):
            if outcome == 'commit_error': raise RuntimeError('test commit failure')
            self.sync_connection.commit()
        async def invalidate(self):
            record.invalidated += 1
            self.sync_connection.invalidate()
    @asynccontextmanager
    async def connect():
        with engine.connect() as sync:
            try: yield Connection(sync)
            finally: record.exited += 1
    monkeypatch.setattr(ownership, '_execution_engine', lambda _db: NS(connect=connect))
    rid = uuid4()
    async def run():
        async with ownership.batch_execution_lease(NS(), rid) as lease:
            if outcome == 'busy':
                assert lease is None
                return
            assert ownership.require_batch_lease(rid) is lease
            if outcome == 'cancel_body': raise asyncio.CancelledError()
            if outcome == 'body_error': raise ValueError('test model error')
    try:
        if outcome in ('success', 'busy'): await run()
        else:
            expected = (asyncio.CancelledError if outcome.startswith('cancel') else
                ValueError if outcome == 'body_error' else RuntimeError)
            with pytest.raises(expected): await run()
        assert record.invalidated == record.exited == 1
        if outcome != 'non_postgres': assert record.key == ownership.batch_lock_key(rid)
        with pytest.raises(ownership.StaleBatchExecution): ownership.require_batch_lease(rid)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_release_the_batch_scope_before_connection_cleanup(tmp_path, monkeypatch):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "batch-cancel.sqlite"}')
    entered, release = asyncio.Event(), asyncio.Event()
    record = NS(invalidated=False, exited=False, cleanup_cancelled=False)
    class Connection:
        dialect = NS(name='postgresql')
        def __init__(self, sync): self.sync_connection = sync
        async def scalar(self, *_args): return True
        async def commit(self): self.sync_connection.commit()
        async def invalidate(self):
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError:
                record.cleanup_cancelled = True
                raise
            self.sync_connection.invalidate()
            record.invalidated = True
    @asynccontextmanager
    async def connect():
        with engine.connect() as sync:
            try: yield Connection(sync)
            finally: record.exited = True
    monkeypatch.setattr(ownership, '_execution_engine', lambda _db: NS(connect=connect))
    async def run():
        async with ownership.batch_execution_lease(NS(), uuid4()): pass
    actor = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        actor.cancel()
        await asyncio.sleep(0)
        actor.cancel()
        await asyncio.sleep(0)
        assert not actor.done() and not record.exited
        release.set()
        with pytest.raises(asyncio.CancelledError): await actor
        assert record.invalidated and record.exited and not record.cleanup_cancelled
    finally:
        release.set()
        await asyncio.gather(actor, return_exceptions=True)
        engine.dispose()
