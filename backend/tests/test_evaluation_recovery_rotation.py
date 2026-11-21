"""Short scan claims must not rewrite worker progress or hold locks to publish."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from app.services import evaluation_recovery as recovery


@pytest.mark.asyncio
async def test_claim_reserves_scan_window_and_preserves_worker_state(monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=5)
    row = NS(id=uuid4(), status='running', updated_at=old, recovery_checked_at=None)
    db = NS(scalars=AsyncMock(return_value=NS(all=lambda: [row])), commit=AsyncMock())
    closed = []
    @asynccontextmanager
    async def sessions():
        yield db
        closed.append(True)
    monkeypatch.setattr(recovery, 'utcnow', lambda: now)
    assert await recovery.claim_recovery_batch(sessions, cutoff=old, limit=20) == [row.id]
    assert closed and row.recovery_checked_at == now
    assert row.updated_at == old and row.status == 'running'
    db.commit.assert_awaited_once()
    sql = str(db.scalars.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    assert 'FOR UPDATE SKIP LOCKED' in sql
    assert 'ORDER BY coalesce(evaluate_tasks.recovery_checked_at, evaluate_tasks.created_at), evaluate_tasks.id' in sql
    assert 'CASE' not in sql  # no pending-first starvation
    assert 'LIMIT 20' in sql


@pytest.mark.asyncio
async def test_claim_commit_failure_aborts_before_worker_lock_or_publish(monkeypatch):
    db = NS(scalars=AsyncMock(return_value=NS(all=lambda: [])), commit=AsyncMock(side_effect=RuntimeError('commit')))
    @asynccontextmanager
    async def sessions(): yield db
    publish = AsyncMock()
    with pytest.raises(RuntimeError, match='commit'):
        await recovery.recover_evaluation_tasks(sessions, dispatch=publish)
    publish.assert_not_awaited()
