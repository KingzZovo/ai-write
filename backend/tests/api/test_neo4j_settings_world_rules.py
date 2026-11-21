"""World-rule update/delete on /api/projects/{pid}/neo4j-settings/world-rules.

Offline: the Neo4j driver is a small in-memory node store injected through
FastAPI dependency_overrides (get_neo4j); the Postgres materialization and
the PG convergence helpers (imported into app.api.neo4j_settings) are
AsyncMocks — following tests/api/test_neo4j_settings_relationships.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from app.main import app
from app.models.project import Project
from app.services.world_rule_material import rule_from_properties, world_rule_version
from tests.support.world_rules import RuleGraph
from tests.test_generation_execution import LocalSession

PID = str(uuid.uuid4())
URL = f"/api/projects/{PID}/neo4j-settings/world-rules"


def _rule(category="power_system", text="灵力枯竭后无法即时恢复", **props):
    return {"id": str(uuid.uuid4()), "category": category, "text": text, **props}


def _version(neo_env):
    return world_rule_version(rule_from_properties(neo_env.driver.rules[0]))


@pytest.fixture
def neo_env(monkeypatch, tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "rule-api.sqlite"}')
    Project.__table__.create(engine)
    with engine.begin() as db:
        db.execute(sa.insert(Project).values(id=uuid.UUID(PID), title='规则编辑'))
    async def get_session():
        with Session(engine) as db:
            yield LocalSession(db)
    driver = RuleGraph(PID, [_rule()])
    async def get_driver(): return driver
    app.dependency_overrides[get_neo4j] = get_driver
    app.dependency_overrides[get_db] = get_session
    materialize = AsyncMock(return_value={})
    pg_update = AsyncMock(return_value=None)
    pg_delete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.api.neo4j_settings._materialize_entities_to_postgres", materialize
    )
    monkeypatch.setattr(
        "app.api.neo4j_settings._update_world_rule_row_in_postgres", pg_update
    )
    monkeypatch.setattr(
        "app.api.neo4j_settings._delete_world_rule_row_from_postgres", pg_delete
    )
    yield SimpleNamespace(
        driver=driver, materialize=materialize, pg_update=pg_update, pg_delete=pg_delete
    )
    app.dependency_overrides.pop(get_neo4j, None)
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


@pytest.mark.asyncio
async def test_put_rewrites_text_in_place_and_converges_pg(auth_client, neo_env):
    old_id = neo_env.driver.rules[0]['id']
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "灵力枯竭后无法即时恢复",
            "new_text": "灵力枯竭后需七日方可恢复",
            "expected_version": _version(neo_env),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {
        "status": "accepted",
        "entity": "world_rule",
        "category": "power_system",
        "text": "灵力枯竭后需七日方可恢复",
        "deduplicated": False,
    }
    # Same node, rewritten in place (no create-new).
    assert neo_env.driver.rules == [
        {"id": old_id, "category": "power_system", "text": "灵力枯竭后需七日方可恢复", "origin": "author"}
    ]
    neo_env.pg_update.assert_awaited_once_with(
        project_id=PID,
        category="power_system",
        old_text="灵力枯竭后无法即时恢复",
        new_category="power_system",
        new_text="灵力枯竭后需七日方可恢复",
    )
    neo_env.materialize.assert_awaited_once()
    assert (
        neo_env.materialize.await_args.kwargs["caller"]
        == "api.neo4j_settings.world_rules.update"
    )


@pytest.mark.asyncio
async def test_put_moves_rule_to_new_category(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "灵力枯竭后无法即时恢复",
            "new_text": "灵力枯竭后无法即时恢复",
            "new_category": "geography",
            "expected_version": _version(neo_env),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["category"] == "geography"
    assert neo_env.driver.rules[0]["category"] == "geography"


@pytest.mark.asyncio
async def test_put_dedupes_into_existing_target_node(auth_client, neo_env):
    target = _rule(text="灵力枯竭后需七日方可恢复", source_outline_id=str(uuid.uuid4()))
    neo_env.driver.rules.append(target)
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "灵力枯竭后无法即时恢复",
            "new_text": "灵力枯竭后需七日方可恢复",
            "expected_version": _version(neo_env),
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["deduplicated"] is True
    # Old node deleted; only the pre-existing target remains — no duplicate.
    assert neo_env.driver.rules == [{**target, 'origin': 'author'}]
    neo_env.pg_update.assert_awaited_once()
    neo_env.materialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_404_when_rule_missing(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "不存在的规则",
            "new_text": "新文本",
            "expected_version": _version(neo_env),
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "world_rule_not_found"
    neo_env.pg_update.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.rules) == 1


@pytest.mark.asyncio
async def test_delete_via_query_params_removes_and_cleans_pg(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"category": "power_system", "text": "灵力枯竭后无法即时恢复", "expected_version": _version(neo_env)}
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["deleted"] == 1
    assert neo_env.driver.rules == []
    neo_env.pg_delete.assert_awaited_once_with(
        project_id=PID,
        category="power_system",
        rule_text="灵力枯竭后无法即时恢复",
    )
    neo_env.materialize.assert_awaited_once()
    assert (
        neo_env.materialize.await_args.kwargs["caller"]
        == "api.neo4j_settings.world_rules.delete"
    )


@pytest.mark.asyncio
async def test_delete_via_json_body(auth_client, neo_env):
    resp = await auth_client.request(
        "DELETE",
        URL,
        json={"category": "power_system", "text": "灵力枯竭后无法即时恢复", "expected_version": _version(neo_env)},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["deleted"] == 1
    assert neo_env.driver.rules == []


@pytest.mark.asyncio
async def test_delete_404_when_rule_missing(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"category": "geography", "text": "灵力枯竭后无法即时恢复", "expected_version": _version(neo_env)}
    )
    assert resp.status_code == 404, resp.text
    neo_env.pg_delete.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.rules) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
async def test_missing_precondition_is_explicit_and_does_not_write(auth_client, neo_env, method):
    response = await auth_client.request(method, URL, json={'category': 'power_system',
        'text': '灵力枯竭后无法即时恢复', 'old_text': '灵力枯竭后无法即时恢复', 'new_text': '不可覆盖'})
    assert response.status_code == 428
    assert response.json()['detail']['code'] == 'world_rule_precondition_required'
    assert not neo_env.driver.calls
    neo_env.materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_project_path_returns_validation_error_before_graph_write(auth_client, neo_env):
    response = await auth_client.post('/api/projects/not-a-uuid/neo4j-settings/world-rules',
        json={'category': '渡口', 'rule_text': '洪水期间停航。'})
    assert response.status_code == 422
    assert not neo_env.driver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
async def test_legacy_uncategorized_full_key_survives_http_boundary(auth_client, neo_env, method):
    row = neo_env.driver.rules[0]
    row.update(category='', text='  原规则第一行。\n末行仍要核对。\n')
    body = {'category': '', 'text': row['text'], 'old_text': row['text'],
            'new_category': '能力限制', 'new_text': '修订后第一行。\n末行仍保留。', 'expected_version': _version(neo_env)}
    response = await auth_client.request(method, URL, json=body)
    assert response.status_code == 202, response.text
    if method == 'DELETE':
        assert neo_env.driver.rules == []
    else:
        assert neo_env.driver.rules[0]['text'] == body['new_text']


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['POST', 'GET'])
async def test_project_read_failure_is_retryable_and_has_no_graph_side_effects(auth_client, neo_env, method):
    async def unavailable():
        yield SimpleNamespace(scalar=AsyncMock(side_effect=sa.exc.SQLAlchemyError('private database details')))
    app.dependency_overrides[get_db] = unavailable
    response = await auth_client.request(method, URL if method == 'POST' else f'/api/projects/{PID}/world-rules',
        json={'category': '渡口', 'rule_text': '封航。'} if method == 'POST' else None)
    assert response.status_code == 503 and 'private' not in response.text
    assert not neo_env.driver.calls
    neo_env.materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
async def test_confirmed_graph_write_survives_an_unavailable_compatibility_projection(auth_client, neo_env, method):
    from app.services.entity_materialization_lock import MaterializationBusy
    neo_env.materialize.side_effect = MaterializationBusy('projection is busy')
    body = {'category': 'power_system', 'rule_text': '新的规则。', 'new_text': '新的规则。',
            'old_text': '灵力枯竭后无法即时恢复', 'text': '灵力枯竭后无法即时恢复', 'expected_version': _version(neo_env)}
    response = await auth_client.request(method, URL, json=body)
    assert response.status_code == 202 and response.json()['status'] == 'accepted'
    assert neo_env.materialize.await_args.kwargs['graph_driver'] is neo_env.driver
    if method == 'DELETE':
        assert neo_env.driver.rules == []
    else:
        assert any(row['text'] == '新的规则。' and row['origin'] == 'author' for row in neo_env.driver.rules)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
async def test_complete_long_extracted_category_can_be_corrected_or_removed(auth_client, neo_env, method):
    row = neo_env.driver.rules[0]
    row['category'] = '原提取分类保留完整来历说明' * 20
    response = await auth_client.request(method, URL, json={'category': row['category'],
        'old_text': row['text'], 'text': row['text'], 'new_category': '能力限制', 'new_text': row['text'],
        'expected_version': _version(neo_env)})
    assert response.status_code == 202, response.text
    if method == 'DELETE':
        assert neo_env.driver.rules == []
    else:
        assert neo_env.driver.rules[0]['category'] == '能力限制'


@pytest.mark.asyncio
async def test_pg_update_uses_the_confirmed_canonical_category_after_normalization(auth_client, neo_env):
    row = neo_env.driver.rules[0]
    row['category'] = '  能力限制  '
    response = await auth_client.put(URL, json={'category': row['category'], 'old_text': row['text'],
        'new_text': '核对后的完整规则。', 'expected_version': _version(neo_env)})
    assert response.status_code == 202, response.text
    assert neo_env.driver.rules[0]['category'] == '能力限制'
    assert neo_env.pg_update.await_args.kwargs['new_category'] == response.json()['category'] == '能力限制'
