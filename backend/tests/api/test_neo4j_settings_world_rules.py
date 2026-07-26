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

from app.db.neo4j import get_neo4j
from app.main import app

PID = str(uuid.uuid4())
URL = f"/api/projects/{PID}/neo4j-settings/world-rules"


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
        if "WorldRule" not in query:
            return _FakeResult()
        if "RETURN count(w) AS existing" in query:
            n = sum(
                1
                for r in d.rules
                if (r["category"], r["text"]) == (kw["new_cat"], kw["new_txt"])
            )
            return _FakeResult({"existing": n})
        key_txt = kw.get("old_txt") if "old_txt" in kw else kw.get("txt")
        matched = [
            r for r in d.rules if (r["category"], r["text"]) == (kw.get("cat"), key_txt)
        ]
        if "DETACH DELETE w" in query:
            for r in matched:
                d.rules.remove(r)
            name = "deleted" if "AS deleted" in query else "matched"
            return _FakeResult({name: len(matched)})
        if "SET w.category" in query:
            for r in matched:
                r["category"] = kw["new_cat"]
                r["text"] = kw["new_txt"]
            return _FakeResult({"matched": len(matched)})
        return _FakeResult()


class _FakeDriver:
    def __init__(self, rules=None):
        self.rules = [dict(r) for r in (rules or [])]
        self.calls: list[tuple[str, dict]] = []

    def session(self, *args, **kwargs):
        return _FakeSession(self)


def _rule(category="power_system", text="灵力枯竭后无法即时恢复", **props):
    return {"category": category, "text": text, **props}


@pytest.fixture
def neo_env(monkeypatch):
    driver = _FakeDriver([_rule()])
    app.dependency_overrides[get_neo4j] = lambda: driver
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


@pytest.mark.asyncio
async def test_put_rewrites_text_in_place_and_converges_pg(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "灵力枯竭后无法即时恢复",
            "new_text": "灵力枯竭后需七日方可恢复",
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
        {"category": "power_system", "text": "灵力枯竭后需七日方可恢复"}
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
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["category"] == "geography"
    assert neo_env.driver.rules[0]["category"] == "geography"


@pytest.mark.asyncio
async def test_put_dedupes_into_existing_target_node(auth_client, neo_env):
    neo_env.driver.rules.append(_rule(text="灵力枯竭后需七日方可恢复"))
    resp = await auth_client.put(
        URL,
        json={
            "category": "power_system",
            "old_text": "灵力枯竭后无法即时恢复",
            "new_text": "灵力枯竭后需七日方可恢复",
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["deduplicated"] is True
    # Old node deleted; only the pre-existing target remains — no duplicate.
    assert neo_env.driver.rules == [
        {"category": "power_system", "text": "灵力枯竭后需七日方可恢复"}
    ]
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
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "world_rule_not_found"
    neo_env.pg_update.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.rules) == 1


@pytest.mark.asyncio
async def test_delete_via_query_params_removes_and_cleans_pg(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"category": "power_system", "text": "灵力枯竭后无法即时恢复"}
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
        json={"category": "power_system", "text": "灵力枯竭后无法即时恢复"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["deleted"] == 1
    assert neo_env.driver.rules == []


@pytest.mark.asyncio
async def test_delete_404_when_rule_missing(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"category": "geography", "text": "灵力枯竭后无法即时恢复"}
    )
    assert resp.status_code == 404, resp.text
    neo_env.pg_delete.assert_not_awaited()
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.rules) == 1
