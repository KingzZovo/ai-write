"""Recovery deadlines must retain durable work at every cancellable phase."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import recovery_deadline
from app.services.character_deletion import delete_character_graph, replay_character_deletions
from app.services.character_profile_recovery import replay_author_profiles
from tests.test_character_deletion import Driver as DeletionDriver
from tests.test_character_profile_recovery import RecoveryDriver


async def setup_recovery(kind):
    if kind == 'profile':
        driver = RecoveryDriver()
        return driver, replay_author_profiles, lambda: all(r['token'] for r in driver.rows)
    driver = DeletionDriver()
    await delete_character_graph(driver, uuid4(), '周砚', {'identity': '师父'}, uuid4())
    return driver, replay_character_deletions, lambda: all(r['status'] == 'pending' for r in driver.receipts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'deletion'])
@pytest.mark.parametrize('phase', ['selection', 'materialize', 'defer'])
async def test_batch_timeout_preserves_work_and_waits_for_cooperative_cleanup(monkeypatch, kind, phase):
    monkeypatch.setattr(recovery_deadline, 'RECOVERY_BATCH_SECONDS', 0.02)
    driver, replay, pending = await setup_recovery(kind)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def hang():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    original_run = driver.run
    async def run(query, **params):
        selection = 'RETURN c.project_id AS project_id' in query or 'collect(d.token)' in query
        defer = 'SET c.profile_sync_attempts=' in query or 'SET d.retry_after=' in query
        if (phase == 'selection' and selection) or (phase == 'defer' and defer):
            await hang()
        return await original_run(query, **params)
    driver.run = run

    async def materialize(**kwargs):
        if phase == 'materialize': await hang()
        if phase == 'defer': raise RuntimeError('retryable failure')
    apply = AsyncMock(side_effect=materialize)
    with pytest.raises(TimeoutError):
        await replay(driver, materialize=apply)
    assert entered.is_set() and cleaned.is_set() and pending()
    if phase == 'selection': apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['profile', 'deletion'])
async def test_external_cancel_is_not_converted_to_batch_timeout(monkeypatch, kind):
    monkeypatch.setattr(recovery_deadline, 'RECOVERY_BATCH_SECONDS', 60)
    driver, replay, pending = await setup_recovery(kind)
    entered = asyncio.Event()
    async def materialize(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(replay(driver, materialize=materialize))
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert pending()


@pytest.mark.asyncio
async def test_timeout_after_one_project_keeps_completed_ack_and_remaining_tokens(monkeypatch):
    monkeypatch.setattr(recovery_deadline, 'RECOVERY_BATCH_SECONDS', 0.02)
    driver = RecoveryDriver()
    driver.rows[1]['project_id'] = 'second'
    async def materialize(**kwargs):
        if kwargs['project_id'] == 'second': await asyncio.Event().wait()
    with pytest.raises(TimeoutError):
        await replay_author_profiles(driver, materialize=materialize)
    assert driver.rows[0]['token'] is None
    assert driver.rows[1]['token'] == '周砚'


@pytest.mark.asyncio
async def test_timeout_during_acknowledgement_retains_profile_receipts(monkeypatch):
    monkeypatch.setattr(recovery_deadline, 'RECOVERY_BATCH_SECONDS', 0.02)
    driver = RecoveryDriver()
    run = driver.run
    async def hang_ack(query, **params):
        if 'RETURN count(c) AS acknowledged' in query: await asyncio.Event().wait()
        return await run(query, **params)
    driver.run = hang_ack
    with pytest.raises(TimeoutError):
        await replay_author_profiles(driver, materialize=AsyncMock())
    assert all(row['token'] for row in driver.rows)


def test_periodic_recovery_messages_expire_before_becoming_stale_backlog():
    from app.tasks import celery_app
    for name in ('author-profile-recovery', 'character-deletion-recovery'):
        entry = celery_app.conf.beat_schedule[name]
        assert 0 < entry['options']['expires'] <= entry['schedule']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['snapshot', 'commit'])
async def test_projector_cancel_exits_session_without_acknowledging(monkeypatch, phase):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.tasks import entity_tasks

    entered, exited = asyncio.Event(), asyncio.Event()
    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    db = SimpleNamespace(scalar=AsyncMock(return_value=True), commit=AsyncMock(), rollback=AsyncMock())
    if phase == 'commit': db.commit = hang
    @asynccontextmanager
    async def sessions():
        try: yield db
        finally: exited.set()
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    monkeypatch.setattr('app.services.character_deletion.stage_character_deletions', AsyncMock(return_value=['token']))
    acknowledge = AsyncMock()
    monkeypatch.setattr('app.services.character_deletion.acknowledge_character_deletions', acknowledge)
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', hang if phase == 'snapshot' else AsyncMock(return_value={}))
    task = asyncio.create_task(entity_tasks._materialize_entities_to_postgres(
        project_id=str(uuid4()), chapter_idx=0, caller='test', strict=True, graph_driver=object()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert exited.is_set()
    acknowledge.assert_not_awaited()
