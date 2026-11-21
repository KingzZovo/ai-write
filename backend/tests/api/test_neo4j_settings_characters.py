"""HTTP deletion contract; transaction and journal behavior have separate tests."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from app.api.neo4j_settings import router
from app.services.character_profile_write import CharacterProfileConflict
from tests.support.character_project import CharacterProjectDB

PID = str(uuid.uuid4())
URL = f'/api/projects/{PID}/neo4j-settings/characters'
app = FastAPI()
app.include_router(router)


@pytest_asyncio.fixture
async def router_client():
    # Isolated router contract: no production auth/rate-limit network calls.
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client


@pytest.fixture
def neo_env(monkeypatch):
    driver = object()
    db = CharacterProjectDB(PID)
    async def get_driver(): return driver
    async def database(): yield db
    app.dependency_overrides[get_neo4j] = get_driver
    app.dependency_overrides[get_db] = database
    materialize, remove = AsyncMock(return_value={}), AsyncMock()
    monkeypatch.setattr('app.api.neo4j_settings._materialize_entities_to_postgres', materialize)
    monkeypatch.setattr('app.services.character_deletion.delete_character_graph', remove)
    yield SimpleNamespace(driver=driver, db=db, materialize=materialize, remove=remove,
        body={'name': '林昭', 'expected_profile_json': {}, 'deletion_token': str(uuid.uuid4())})
    app.dependency_overrides.pop(get_neo4j, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_delete_requires_a_baseline_and_idempotency_token(router_client, neo_env):
    response = await router_client.request('DELETE', URL, json=neo_env.body)
    assert response.status_code == 202
    assert response.json() == {'status': 'accepted', 'entity': 'character', 'name': '林昭', 'deleted': 1}
    neo_env.remove.assert_awaited_once_with(neo_env.driver, PID, '林昭', {}, uuid.UUID(neo_env.body['deletion_token']))
    assert neo_env.materialize.call_args.kwargs['strict'] is True
    assert neo_env.materialize.call_args.kwargs['graph_driver'] is neo_env.driver


@pytest.mark.asyncio
async def test_unconditional_legacy_delete_is_rejected(router_client, neo_env):
    response = await router_client.delete(URL, params={'name': '林昭'})
    assert response.status_code == 422
    neo_env.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_delete_returns_conflict_without_projection(router_client, neo_env):
    neo_env.remove.side_effect = CharacterProfileConflict('人物已变化')
    response = await router_client.request('DELETE', URL, json=neo_env.body)
    assert response.status_code == 409
    neo_env.materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_failure_is_pending_not_success(router_client, neo_env):
    neo_env.materialize.side_effect = RuntimeError('private diagnostic')
    response = await router_client.request('DELETE', URL, json=neo_env.body)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'character_deletion_pending'
    assert 'private diagnostic' not in response.text


@pytest.mark.asyncio
async def test_unavailable_project_cannot_delete_a_graph_character(router_client, neo_env):
    neo_env.db.available = False
    response = await router_client.request('DELETE', URL, json=neo_env.body)
    assert response.status_code == 404 and response.json()['detail']['code'] == 'character_project_unavailable'
    neo_env.remove.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert not neo_env.db.held
