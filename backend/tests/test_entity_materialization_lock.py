from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import asyncio

import pytest

from app.services.entity_materialization_lock import materialization_key, require_materialization_lock, MaterializationBusy


@pytest.fixture(autouse=True)
def no_graph_cleanup(monkeypatch):
    monkeypatch.setattr('app.services.character_deletion.stage_character_deletions', AsyncMock(return_value=[]))


def test_lock_namespace_is_stable_and_separate_from_worker_locks():
    from app.services.production.locking import project_lock_key
    pid = uuid4()
    assert materialization_key(pid) == materialization_key(str(pid))
    assert materialization_key(pid) != project_lock_key(pid)
    assert materialization_key(pid) != materialization_key(uuid4())
    assert -(2**63) <= materialization_key(pid) < 2**63


@pytest.mark.asyncio
async def test_transaction_lock_never_commits_or_releases_before_snapshot():
    db = SimpleNamespace(scalar=AsyncMock(return_value=True), commit=AsyncMock())
    await require_materialization_lock(db, uuid4())
    query, params = db.scalar.call_args.args
    assert str(query) == 'SELECT pg_try_advisory_xact_lock(:key)'
    assert isinstance(params['key'], int)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_busy_materializer_never_reads_graph_or_claims_success(monkeypatch, strict):
    from app.tasks import entity_tasks
    db = SimpleNamespace(scalar=AsyncMock(return_value=False))
    @asynccontextmanager
    async def sessions(): yield db
    snapshot = AsyncMock()
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', snapshot)
    with pytest.raises(MaterializationBusy):
        await entity_tasks._materialize_entities_to_postgres(project_id=str(uuid4()), chapter_idx=0,
            caller='test', strict=strict)
    snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_snapshot_uses_locked_write_session_and_scope_closes_on_failure(monkeypatch, cancel):
    from app.tasks import entity_tasks
    events = []
    async def scalar(query, params): events.append('locked'); return True
    db = SimpleNamespace(scalar=scalar)
    @asynccontextmanager
    async def sessions():
        events.append('opened')
        try: yield db
        finally: events.append('closed')
    async def snapshot(session, **kwargs):
        assert session is db
        assert events == ['opened', 'locked']
        events.append('snapshot')
        if cancel: raise asyncio.CancelledError()
        raise RuntimeError('snapshot failure')
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', snapshot)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await entity_tasks._materialize_entities_to_postgres(project_id=str(uuid4()), chapter_idx=0,
            caller='test', strict=True, graph_driver=object())
    assert events == ['opened', 'locked', 'snapshot', 'closed']


@pytest.mark.asyncio
async def test_busy_http_response_is_registered_retryable_and_does_not_expose_diagnostics():
    import json
    from starlette.requests import Request
    from app.main import app, materialization_busy_handler
    assert app.exception_handlers[MaterializationBusy] is materialization_busy_handler
    response = await materialization_busy_handler(Request({'type': 'http'}), MaterializationBusy('private diagnostic'))
    assert response.status_code == 503 and response.headers['Retry-After'] == '2'
    assert json.loads(response.body) == {'detail': '该作品的资料正在同步，请稍后重试。'}
