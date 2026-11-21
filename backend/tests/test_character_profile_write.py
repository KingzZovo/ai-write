"""Conditional-write contracts with a transactional graph test double."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import neo4j_settings as api
from app.services.character_profile_write import write_character_profile, CharacterProfileConflict
from tests.support.character_project import CharacterProjectDB


class Result:
    def __init__(self, row=None): self.row = row
    async def single(self): return self.row
    async def consume(self): pass


class ProfileDriver:
    def __init__(self, profile=None):
        self.row = None if profile is None else {'id': 'existing', 'profile_json': json.dumps(profile)}
        self.calls = []
        self.lock = asyncio.Lock()
    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def execute_write(self, callback):
        async with self.lock:
            before = deepcopy(self.row)
            try: return await callback(self)
            except BaseException:
                self.row = before
                raise
    async def run(self, query, **params):
        self.calls.append((query, params))
        if 'AS pending' in query:
            return Result({'profile_json': self.row['profile_json'], 'pending': bool(self.row.get('profile_sync_token'))} if self.row else None)
        if 'RETURN c.id AS id' in query:
            if query.startswith('MERGE') and self.row is None:
                self.row = {'id': params['id'], 'profile_json': None}
            return Result(deepcopy(self.row))
        if 'RETURN count(c) AS acknowledged' in query:
            matches = self.row is not None and self.row.get('profile_sync_token') == params['rows'][0]['token']
            if matches: self.row.pop('profile_sync_token', None)
            return Result({'acknowledged': int(matches)})
        self.row['profile_json'] = params['profile']
        self.row['profile_origin'] = 'author_edit'
        self.row['profile_sync_token'] = params['token']
        return Result()


@pytest.mark.asyncio
async def test_stale_edit_and_different_duplicate_create_do_not_write():
    driver = ProfileDriver({'identity': '新稿'})
    for expected in ({'identity': '旧稿'}, None):
        with pytest.raises(CharacterProfileConflict):
            await write_character_profile(driver, 'p', '周砚', {'identity': '不得覆盖'}, expected)
    assert json.loads(driver.row['profile_json']) == {'identity': '新稿'}
    assert all('profile_write_lock' in query for query, _ in driver.calls)


@pytest.mark.asyncio
async def test_create_edit_clear_and_identical_retry():
    driver = ProfileDriver()
    await write_character_profile(driver, 'p', '周砚', {'identity': '师父'}, None)
    await write_character_profile(driver, 'p', '周砚', {}, {'identity': '师父'})
    await write_character_profile(driver, 'p', '周砚', {}, {'identity': '师父'})
    assert json.loads(driver.row['profile_json']) == {}
    assert driver.row['profile_origin'] == 'author_edit'


@pytest.mark.asyncio
async def test_deleted_character_is_not_recreated_by_a_stale_edit():
    driver = ProfileDriver()
    with pytest.raises(CharacterProfileConflict):
        await write_character_profile(driver, 'p', '周砚', {'identity': '旧身份'}, {})
    assert driver.row is None


@pytest.mark.asyncio
async def test_invalid_stored_profile_is_preserved():
    driver = ProfileDriver({})
    driver.row['profile_json'] = 'broken json'
    with pytest.raises(CharacterProfileConflict):
        await write_character_profile(driver, 'p', '周砚', {}, {})
    assert driver.row['profile_json'] == 'broken json'


@pytest.mark.asyncio
async def test_comparison_is_key_order_independent_but_type_sensitive():
    driver = ProfileDriver({'a': True, 'b': 2})
    with pytest.raises(CharacterProfileConflict):
        await write_character_profile(driver, 'p', '周砚', {}, {'b': 2, 'a': 1})
    await write_character_profile(driver, 'p', '周砚', {}, {'b': 2, 'a': True})
    assert json.loads(driver.row['profile_json']) == {}


@pytest.mark.asyncio
async def test_api_conflict_does_not_project_and_projection_failure_is_retryable(monkeypatch):
    pid = str(uuid4()); db = CharacterProjectDB(pid)
    driver = ProfileDriver({'identity': '师父'})
    materialize = AsyncMock(side_effect=RuntimeError('private diagnostic'))
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', materialize)
    stale = api.Neo4jCharacterUpsertRequest(name='周砚', profile_json={}, expected_profile_json={'identity': '旧身份'})
    with pytest.raises(HTTPException) as error: await api.upsert_character(pid, stale, driver, db)
    assert error.value.status_code == 409
    materialize.assert_not_awaited()
    body = api.Neo4jCharacterUpsertRequest(name=' 周砚 ', profile_json={}, expected_profile_json={'identity': '师父'})
    with pytest.raises(HTTPException) as error: await api.upsert_character(pid, body, driver, db)
    assert error.value.status_code == 503 and 'private diagnostic' not in str(error.value.detail)
    assert error.value.detail['code'] == 'character_projection_pending'
    assert driver.row['profile_sync_token']
    materialize.side_effect = None
    materialize.return_value = {}
    assert (await api.upsert_character(pid, body, driver, db))['name'] == '周砚'
    assert 'profile_sync_token' not in driver.row
    assert materialize.call_args.kwargs['strict'] is True
    assert materialize.call_args.kwargs['graph_driver'] is driver


def test_schema_requires_a_baseline_and_rejects_blank_names():
    with pytest.raises(ValidationError): api.Neo4jCharacterUpsertRequest(name='周砚', profile_json={})
    with pytest.raises(ValidationError): api.Neo4jCharacterUpsertRequest(name='周砚', expected_profile_json={})
    with pytest.raises(ValidationError): api.Neo4jCharacterUpsertRequest(name='周砚', profile_json=None, expected_profile_json={})
    with pytest.raises(ValidationError): api.Neo4jCharacterUpsertRequest(name='  ', profile_json={}, expected_profile_json=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [False, True])
async def test_status_reads_current_profile_without_mutation_or_private_token(pending):
    driver = ProfileDriver({'identity': '师父'})
    if pending: driver.row['profile_sync_token'] = 'private-token'
    result = await api.character_profile_status('p', ' 周砚 ', driver)
    assert result == {'name': '周砚', 'profile_json': {'identity': '师父'}, 'pending': pending}
    assert len(driver.calls) == 1 and 'SET ' not in driver.calls[0][0]
    assert driver.calls[0][1] == {'pid': 'p', 'name': '周砚'}


@pytest.mark.asyncio
async def test_status_rejects_missing_and_malformed_profiles():
    driver = ProfileDriver()
    with pytest.raises(HTTPException) as error: await api.character_profile_status('p', '周砚', driver)
    assert error.value.status_code == 404
    driver.row = {'id': 'old', 'profile_json': 'invalid'}
    with pytest.raises(HTTPException) as error: await api.character_profile_status('p', '周砚', driver)
    assert error.value.status_code == 409
