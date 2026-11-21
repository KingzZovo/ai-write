from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.services.character_profile_recovery import replay_author_profiles, now_ms
from app.services.character_profile_write import write_character_profile
from tests.test_character_profile_write import ProfileDriver


class Result:
    def __init__(self, rows): self.rows = rows
    async def data(self): return self.rows
    async def single(self): return self.rows[0] if self.rows else None
    async def consume(self): pass


class RecoveryDriver:
    def __init__(self):
        self.rows = [{'project_id': 'p', 'name': name, 'token': name, 'after': 0} for name in ('林禾', '周砚')]
        self.calls = []
    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def run(self, query, **params):
        self.calls.append((query, params))
        if 'RETURN c.project_id AS project_id' in query:
            return Result(deepcopy([r for r in self.rows if r['token'] and r['after'] <= params['now']][:params['limit']]))
        assert query.index('SET c.profile_write_lock=') < query.index('WHERE c.profile_sync_token=row.token')
        matches = [r for r in self.rows if r['project_id'] == params['pid'] and any(
            r['name'] == item['name'] and r['token'] == item['token'] for item in params['rows'])]
        if 'RETURN count(c) AS acknowledged' in query:
            for row in matches: row['token'] = None
            return Result([{'acknowledged': len(matches)}])
        for row in matches: row['after'] = params['now'] + 60000
        return Result([])


@pytest.mark.asyncio
async def test_profile_write_returns_the_durable_token_from_same_write():
    driver = ProfileDriver()
    token = await write_character_profile(driver, 'p', '周砚', {'identity': '师父'}, None)
    assert token and driver.row['profile_sync_token'] == token
    query, params = driver.calls[-1]
    assert 'c.profile_json=$profile' in query and 'c.profile_sync_token=$token' in query
    assert params['token'] == token


@pytest.mark.asyncio
async def test_recovery_groups_same_project_and_acknowledges_after_strict_projection():
    driver = RecoveryDriver()
    async def apply(**kwargs):
        assert all(row['token'] for row in driver.rows)
        assert kwargs['strict'] is True and kwargs['graph_driver'] is driver
    materialize = AsyncMock(side_effect=apply)
    result = await replay_author_profiles(driver, materialize=materialize)
    assert result == {'examined': 2, 'projects': 1, 'acknowledged': 2, 'failed': 0}
    materialize.assert_awaited_once()
    assert all(row['token'] is None for row in driver.rows)


@pytest.mark.asyncio
async def test_projection_failure_keeps_tokens_and_defers_retry_without_model_work():
    driver = RecoveryDriver()
    result = await replay_author_profiles(driver, materialize=AsyncMock(side_effect=RuntimeError('PG unavailable')))
    assert result['failed'] == 1 and result['acknowledged'] == 0
    assert all(row['token'] and row['after'] > now_ms() for row in driver.rows)
    materialize = AsyncMock()
    assert (await replay_author_profiles(driver, materialize=materialize))['examined'] == 0
    materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_old_batch_cannot_acknowledge_or_delay_newer_author_save(fail):
    driver = RecoveryDriver()
    async def apply(**kwargs):
        driver.rows[0]['token'] = 'new-save'
        if fail: raise RuntimeError('projection failed')
    result = await replay_author_profiles(driver, materialize=apply)
    assert driver.rows[0]['token'] == 'new-save' and driver.rows[0]['after'] == 0
    assert result['acknowledged'] == (0 if fail else 1)


@pytest.mark.asyncio
async def test_cancellation_preserves_pending_work():
    driver = RecoveryDriver()
    with pytest.raises(asyncio.CancelledError):
        await replay_author_profiles(driver, materialize=AsyncMock(side_effect=asyncio.CancelledError()))
    assert all(row['token'] for row in driver.rows)
    assert len(driver.calls) == 1


@pytest.mark.asyncio
async def test_acknowledgement_failure_keeps_work_recoverable():
    driver = RecoveryDriver()
    run = driver.run
    async def fail_ack(query, **params):
        if 'RETURN count(c) AS acknowledged' in query: raise RuntimeError('graph unavailable')
        return await run(query, **params)
    driver.run = fail_ack
    result = await replay_author_profiles(driver, materialize=AsyncMock())
    assert result['failed'] == 1
    assert all(row['token'] for row in driver.rows)


def test_periodic_task_runs_async_recovery(monkeypatch):
    from app import tasks
    from app.tasks import entity_tasks
    from app.db import neo4j
    driver = object()
    recovery = AsyncMock(return_value={'examined': 0})
    monkeypatch.setattr(neo4j, 'init_neo4j', AsyncMock())
    monkeypatch.setattr(neo4j, '_driver', driver)
    monkeypatch.setattr('app.services.character_profile_recovery.replay_author_profiles', recovery)
    monkeypatch.setattr(tasks, '_run_async_safe', asyncio.run)
    assert entity_tasks.replay_author_profiles.run() == {'examined': 0}
    recovery.assert_awaited_once_with(driver)
    assert tasks.celery_app.conf.beat_schedule['author-profile-recovery']['task'] == 'entities.replay_author_profiles'
