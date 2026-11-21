"""Real PG recovery/concurrency gates, deliberately separate from offline tests."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select

from app.models.entity_extraction import OutlineSettingsExtraction as Receipt
from app.models.project import Outline
from app.services.outline_settings_delivery import save_receipt, deliver_receipt, replay_settings, now
from app.services.outline_settings_source import read_settings_source
from tests.test_pipeline_control import runtime_db
from tests.test_volume_creation import creation

PAYLOAD = {'characters': [{'name': '林禾'}], 'world_rules': [], 'relationships': []}
COUNTS = {'chars_created': 1, 'rules_created': 0, 'rels_created': 0}


@pytest_asyncio.fixture
async def receipts(creation):
    r = creation
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Receipt.__table__.create(c))
    async with r.sessions() as db:
        r.source = await read_settings_source(db, r.pid, r.bid)
    return r


@pytest.mark.asyncio
async def test_concurrent_saves_keep_one_payload_and_deliver_once(receipts):
    r = receipts
    async def save(payload):
        async with r.sessions() as db:
            return (await save_receipt(db, r.source, payload)).id
    ids = await asyncio.wait_for(asyncio.gather(save(PAYLOAD), save({**PAYLOAD, 'tag': 'second'})), 5)
    assert ids[0] == ids[1]
    apply = AsyncMock(return_value=COUNTS)
    async def deliver():
        async with r.sessions() as db:
            return (await deliver_receipt(db, await db.get(Receipt, ids[0]), apply=apply)).status
    assert await asyncio.wait_for(asyncio.gather(deliver(), deliver()), 5) == ['completed', 'completed']
    apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_receipt_survives_delivery_commit_failure_and_replays(receipts):
    r = receipts
    async with r.sessions() as db:
        receipt = await save_receipt(db, r.source, PAYLOAD)
        rid = receipt.id
        def fail(session):
            raise RuntimeError('simulated commit failure')
        event.listen(db.sync_session, 'before_commit', fail)
        try:
            with pytest.raises(RuntimeError):
                await deliver_receipt(db, receipt, apply=AsyncMock(return_value=COUNTS))
        finally:
            event.remove(db.sync_session, 'before_commit', fail)
            await db.rollback()
    async with r.sessions() as db:
        persisted = await db.get(Receipt, rid)
        assert persisted.status == 'pending' and persisted.attempts == 0
        assert persisted.payload_json == PAYLOAD
    apply = AsyncMock(return_value=COUNTS)
    assert (await replay_settings(r.sessions, apply=apply))['completed'] == 1
    apply.assert_awaited_once()
    assert (await replay_settings(r.sessions, apply=apply))['examined'] == 0


@pytest.mark.asyncio
async def test_replay_waits_for_backoff_and_stops_after_source_edit(receipts):
    r = receipts
    async with r.sessions() as db:
        receipt = await save_receipt(db, r.source, PAYLOAD)
        rid = receipt.id
        await deliver_receipt(db, receipt, apply=AsyncMock(side_effect=RuntimeError('graph offline')))
    apply = AsyncMock(return_value=COUNTS)
    assert (await replay_settings(r.sessions, apply=apply))['examined'] == 0
    async with r.sessions() as db:
        (await db.get(Receipt, rid)).next_attempt_at = now() - timedelta(seconds=1)
        (await db.get(Outline, r.bid)).content_json = {'raw_text': '作者新稿'}
        await db.commit()
    assert (await replay_settings(r.sessions, apply=apply))['outcomes'] == ['stale']
    apply.assert_not_awaited()
