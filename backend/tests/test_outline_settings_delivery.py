"""Offline delivery contracts; SQL concurrency is covered separately on PG."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import outline_settings_delivery as delivery
from app.services.outline_settings_source import read_settings_source
from tests.test_outline_settings_source import source_db

PAYLOAD = {'characters': [{'name': '林禾'}], 'world_rules': [], 'relationships': []}
COUNTS = {'chars_created': 1, 'rules_created': 0, 'rels_created': 0}


async def prepared():
    project, outline, db = source_db()
    source = await read_settings_source(db, project.id, outline.id)
    receipt = await delivery.save_receipt(db, source, PAYLOAD)
    return project, outline, db, source, receipt


@pytest.mark.asyncio
async def test_payload_is_committed_before_delivery_and_reused_after_failure():
    _, _, db, source, receipt = await prepared()
    db.commit.assert_awaited_once()
    assert receipt.payload_json == PAYLOAD and receipt.payload_json is not PAYLOAD
    before = delivery.now()
    apply = AsyncMock(side_effect=RuntimeError('private diagnostic'))
    result = await delivery.deliver_receipt(db, receipt, apply=apply)
    assert result.status == 'failed' and result.attempts == 1
    assert result.last_error == 'delivery_failed'
    assert result.next_attempt_at > before
    assert result.payload_json == PAYLOAD
    same = await delivery.save_receipt(db, source, {'characters': []})
    assert same is receipt and same.payload_json == PAYLOAD
    apply.side_effect = None
    apply.return_value = COUNTS
    result = await delivery.deliver_receipt(db, same, apply=apply)
    assert result.status == 'completed' and result.attempts == 2
    assert result.counts_json == COUNTS and result.last_error is None
    await delivery.deliver_receipt(db, result, apply=apply)
    assert apply.await_count == 2  # Completed delivery does not run again.


@pytest.mark.asyncio
async def test_obsolete_payload_stops_before_graph_write():
    _, outline, db, _, receipt = await prepared()
    outline.content_json = {'raw_text': '作者已经修改人物身份'}
    apply = AsyncMock()
    result = await delivery.deliver_receipt(db, receipt, apply=apply)
    assert result.status == 'stale' and result.last_error == 'source_changed'
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_does_not_commit_partial_completion():
    _, _, db, _, receipt = await prepared()
    with pytest.raises(asyncio.CancelledError):
        await delivery.deliver_receipt(db, receipt, apply=AsyncMock(side_effect=asyncio.CancelledError))
    db.commit.assert_awaited_once()  # Only the durable payload was committed.
    assert receipt.status == 'pending'


@pytest.mark.asyncio
async def test_periodic_delivery_rechecks_deadline_after_waiting_for_lock():
    _, _, db, _, receipt = await prepared()
    receipt.next_attempt_at = delivery.now() + timedelta(minutes=1)
    receipt.status = 'failed'
    apply = AsyncMock(return_value=COUNTS)
    await delivery.deliver_receipt(db, receipt, apply=apply, due_only=True)
    apply.assert_not_awaited()
    assert receipt.attempts == 0
    await delivery.deliver_receipt(db, receipt, apply=apply)  # Explicit retry is allowed.
    apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_retries_cached_payload_without_model_and_status_exposes_no_payload(monkeypatch):
    from app.api import outlines as api
    project, outline, db, _, receipt = await prepared()
    model = AsyncMock()
    monkeypatch.setattr(api, 'extract_settings_from_outline', model)
    monkeypatch.setattr(api, 'init_neo4j', AsyncMock())
    apply = AsyncMock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(delivery, 'apply_payload', apply)
    with pytest.raises(HTTPException) as exc:
        await api.extract_settings(str(project.id), str(outline.id), db)
    assert exc.value.status_code == 503
    apply.side_effect = None
    apply.return_value = COUNTS
    result = await api.extract_settings(str(project.id), str(outline.id), db)
    assert result.characters_created == 1
    model.assert_not_awaited()
    status = await api.settings_extraction_status(str(project.id), str(outline.id), db)
    assert status == {'status': 'completed', 'attempts': 2, 'counts': {
        'characters_created': 1, 'world_rules_created': 0, 'relationships_created': 0}}
    assert receipt.payload_json == PAYLOAD


@pytest.mark.asyncio
async def test_status_without_receipt_does_not_start_extraction(monkeypatch):
    from app.api import outlines as api
    project, outline, db = source_db()
    model = AsyncMock()
    monkeypatch.setattr(api, 'extract_settings_from_outline', model)
    assert await api.settings_extraction_status(str(project.id), str(outline.id), db) == {'status': 'not_started'}
    model.assert_not_awaited()


def test_scheduled_task_runs_recovery_through_async_bridge(monkeypatch):
    from app import tasks
    from app.tasks.entity_tasks import replay_outline_settings
    from app.db.session import async_session_factory
    recovery = AsyncMock(return_value={'examined': 0})
    monkeypatch.setattr(delivery, 'replay_settings', recovery)
    monkeypatch.setattr(tasks, '_run_async_safe', asyncio.run)
    assert replay_outline_settings.run() == {'examined': 0}
    recovery.assert_awaited_once_with(async_session_factory)


@pytest.mark.asyncio
async def test_replay_continues_after_delivery_error_and_limits_due_work(monkeypatch):
    _, _, db, _, receipt = await prepared()
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [receipt.id, receipt.id]))
    db.get = AsyncMock(return_value=receipt)
    @asynccontextmanager
    async def sessions():
        yield db
    deliver = AsyncMock(side_effect=[RuntimeError('commit lost'), receipt])
    receipt.status = 'completed'
    monkeypatch.setattr(delivery, 'deliver_receipt', deliver)
    result = await delivery.replay_settings(sessions, limit=2)
    assert result == {'examined': 2, 'completed': 1, 'outcomes': ['retry_later', 'completed']}
    from sqlalchemy.dialects import postgresql
    query = db.scalars.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    sql = str(query)
    assert "IN ('pending', 'failed')" in sql and 'next_attempt_at <=' in sql and 'LIMIT 2' in sql
