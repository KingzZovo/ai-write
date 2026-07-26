"""Character delete on /api/projects/{pid}/neo4j-settings/characters.

Offline: the Neo4j driver is a small in-memory node store injected through
FastAPI dependency_overrides (get_neo4j); the Postgres materialization and
the PG cleanup helper (imported into app.api.neo4j_settings) are AsyncMocks —
following tests/api/test_neo4j_settings_relationships.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.neo4j import get_neo4j
from app.main import app

PID = str(uuid.uuid4())
URL = f"/api/projects/{PID}/neo4j-settings/characters"


class _FakeResult:
    def __init__(self, record=None):
        self._record = record

    async def consume(self):
        return None

    async def single(self):
        return self._record


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, query, **kw):
        d = self._driver
        d.calls.append((query, kw))
        matched = [c for c in d.chars if c["name"] == kw.get("name")]
        if "HAS_STATE" in query and "DETACH DELETE s" in query:
            n = sum(c["states"] for c in matched)
            for c in matched:
                c["states"] = 0
            return _FakeResult({"deleted_states": n})
        if "DETACH DELETE c" in query:
            for c in matched:
                d.chars.remove(c)
            return _FakeResult({"deleted": len(matched)})
        return _FakeResult()


class _FakeDriver:
    def __init__(self, chars=None):
        self.chars = [dict(c) for c in (chars or [])]
        self.calls: list[tuple[str, dict]] = []

    def session(self, *args, **kwargs):
        return _FakeSession(self)


@pytest.fixture
def neo_env(monkeypatch):
    driver = _FakeDriver([{"name": "林昭", "states": 2}])
    app.dependency_overrides[get_neo4j] = lambda: driver
    materialize = AsyncMock(return_value={})
    pg_cleanup = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.api.neo4j_settings._materialize_entities_to_postgres", materialize
    )
    monkeypatch.setattr(
        "app.api.neo4j_settings._delete_character_rows_from_postgres", pg_cleanup
    )
    yield SimpleNamespace(driver=driver, materialize=materialize, pg_cleanup=pg_cleanup)
    app.dependency_overrides.pop(get_neo4j, None)


@pytest.mark.asyncio
async def test_delete_via_query_param_removes_node_states_and_pg_rows(
    auth_client, neo_env
):
    resp = await auth_client.delete(URL, params={"name": "林昭"})
    assert resp.status_code == 202, resp.text
    assert resp.json() == {
        "status": "accepted",
        "entity": "character",
        "name": "林昭",
        "deleted": 1,
    }
    assert neo_env.driver.chars == []
    # HAS_STATE state nodes are deleted explicitly before the character node.
    state_calls = [q for q, _ in neo_env.driver.calls if "HAS_STATE" in q]
    assert len(state_calls) == 1
    neo_env.pg_cleanup.assert_awaited_once_with(project_id=PID, name="林昭")
    neo_env.materialize.assert_awaited_once()
    assert (
        neo_env.materialize.await_args.kwargs["caller"]
        == "api.neo4j_settings.characters.delete"
    )


@pytest.mark.asyncio
async def test_delete_via_json_body(auth_client, neo_env):
    resp = await auth_client.request("DELETE", URL, json={"name": "林昭"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["deleted"] == 1
    assert neo_env.driver.chars == []


@pytest.mark.asyncio
async def test_delete_404_when_character_missing(auth_client, neo_env):
    resp = await auth_client.delete(URL, params={"name": "不存在的人"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "character_not_found"
    neo_env.pg_cleanup.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.chars) == 1


@pytest.mark.asyncio
async def test_delete_422_without_name(auth_client, neo_env):
    resp = await auth_client.delete(URL)
    assert resp.status_code == 422, resp.text
    neo_env.materialize.assert_not_awaited()
