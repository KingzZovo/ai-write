"""Relationship edit/delete on /api/projects/{pid}/neo4j-settings.

Offline: the Neo4j driver is a small in-memory edge store injected through
FastAPI dependency_overrides (get_neo4j), and the Postgres materialization
(_materialize_entities_to_postgres, imported into app.api.neo4j_settings)
is an AsyncMock — following the fake-driver patterns of
tests/test_global_chapter_idx.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.neo4j import get_neo4j
from app.main import app

PID = str(uuid.uuid4())
URL = f"/api/projects/{PID}/neo4j-settings/relationships"


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

    @staticmethod
    def _matches(edge, kw):
        types = {kw.get("rtype"), kw.get("raw_rtype")} - {None}
        return (
            edge["src"] == kw.get("src")
            and edge["tgt"] == kw.get("tgt")
            and edge["props"].get("type") in types
        )

    async def run(self, query, **kw):
        d = self._driver
        d.calls.append((query, kw))
        if "RELATES_TO" not in query:
            return _FakeResult()
        matched = [e for e in d.edges if self._matches(e, kw)]
        if "DELETE r" in query:
            for e in matched:
                d.edges.remove(e)
            return _FakeResult({"deleted": len(matched)})
        if "RETURN count(r) AS matched" in query:
            for e in matched:
                if "new_rtype" in kw:
                    e["props"]["type"] = kw["new_rtype"]
                    e["props"]["raw_type"] = kw["new_raw_type"]
                for f in ("label", "note", "sentiment"):
                    if f in kw and f"r.{f} = ${f}" in query:
                        e["props"][f] = kw[f]
            return _FakeResult({"matched": len(matched)})
        return _FakeResult()


class _FakeDriver:
    def __init__(self, edges=None):
        self.edges = [dict(e) for e in (edges or [])]
        self.calls: list[tuple[str, dict]] = []

    def session(self, *args, **kwargs):
        return _FakeSession(self)


def _edge(src="林昭", tgt="顾长风", rtype="同伴", **props):
    return {"src": src, "tgt": tgt, "props": {"type": rtype, "chapter_start": 3, **props}}


@pytest.fixture
def neo_env(monkeypatch):
    driver = _FakeDriver([_edge()])
    app.dependency_overrides[get_neo4j] = lambda: driver
    materialize = AsyncMock(return_value={})
    monkeypatch.setattr(
        "app.api.neo4j_settings._materialize_entities_to_postgres", materialize
    )
    yield SimpleNamespace(driver=driver, materialize=materialize)
    app.dependency_overrides.pop(get_neo4j, None)


@pytest.mark.asyncio
async def test_put_updates_properties_and_rematerializes(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "source": "林昭",
            "target": "顾长风",
            "rel_type": "同伴",
            "label": "挚友",
            "note": "北境同行结识",
            "sentiment": "positive",
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {
        "status": "accepted",
        "entity": "relationship",
        "source": "林昭",
        "target": "顾长风",
        "rel_type": "同伴",
    }
    props = neo_env.driver.edges[0]["props"]
    assert props["label"] == "挚友"
    assert props["note"] == "北境同行结识"
    assert props["sentiment"] == "positive"
    assert props["type"] == "同伴"  # unchanged without new_rel_type
    neo_env.materialize.assert_awaited_once()
    assert (
        neo_env.materialize.await_args.kwargs["caller"]
        == "api.neo4j_settings.relationships.update"
    )


@pytest.mark.asyncio
async def test_put_renames_rel_type_preserving_other_properties(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "source": "林昭",
            "target": "顾长风",
            "rel_type": "同伴",
            "new_rel_type": "敌对",
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["rel_type"] == "敌对"
    props = neo_env.driver.edges[0]["props"]
    assert props["type"] == "敌对"
    assert props["raw_type"] == "敌对"
    # Rename is a property SET on the RELATES_TO edge, so unrelated
    # properties survive untouched.
    assert props["chapter_start"] == 3


@pytest.mark.asyncio
async def test_put_matches_verbose_rel_type_via_canonicalization(auth_client, neo_env):
    """The stored r.type is canonical ('同伴'); a verbose user-supplied
    rel_type ('同伴（并肩作战）') must still match."""
    resp = await auth_client.put(
        URL,
        json={
            "source": "林昭",
            "target": "顾长风",
            "rel_type": "同伴（并肩作战）",
            "label": "战友",
        },
    )
    assert resp.status_code == 202, resp.text
    assert neo_env.driver.edges[0]["props"]["label"] == "战友"


@pytest.mark.asyncio
async def test_put_404_when_edge_missing(auth_client, neo_env):
    resp = await auth_client.put(
        URL,
        json={
            "source": "林昭",
            "target": "不存在的人",
            "rel_type": "同伴",
            "label": "x",
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "relationship_not_found"
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.edges) == 1


@pytest.mark.asyncio
async def test_delete_via_query_params_removes_and_rematerializes(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"source": "林昭", "target": "顾长风", "rel_type": "同伴"}
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["deleted"] == 1
    assert neo_env.driver.edges == []
    neo_env.materialize.assert_awaited_once()
    assert (
        neo_env.materialize.await_args.kwargs["caller"]
        == "api.neo4j_settings.relationships.delete"
    )


@pytest.mark.asyncio
async def test_delete_via_json_body(auth_client, neo_env):
    resp = await auth_client.request(
        "DELETE",
        URL,
        json={"source": "林昭", "target": "顾长风", "rel_type": "同伴"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["deleted"] == 1
    assert neo_env.driver.edges == []


@pytest.mark.asyncio
async def test_delete_404_when_edge_missing(auth_client, neo_env):
    resp = await auth_client.delete(
        URL, params={"source": "林昭", "target": "顾长风", "rel_type": "师生"}
    )
    assert resp.status_code == 404, resp.text
    neo_env.materialize.assert_not_awaited()
    assert len(neo_env.driver.edges) == 1
