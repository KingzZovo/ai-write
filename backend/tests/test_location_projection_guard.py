"""同章两个地点不能让整章实体投影失败。

``AT_LOCATION`` 上有唯一约束 ``(project_id, character_name, chapter_start)``。
旧写法对同键边做 ``MERGE`` 时,若终点不同就匹配不到已有边,于是试图新建同键的边,
Neo4j 抛 ``ConstraintValidationFailed``。实测(2026-11-14 连续 24 章)第 11 章
周芸先在医生办公室、后回病房,那一章的实体投影**整章丢失**。

第一个用例是结构守卫(不连库);第二个用例连真实 Neo4j,离线档跳过。
"""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.entity_graph_projection import VersionedTimeline

class _Tx:
    def __init__(self):
        self.queries = []

    async def run(self, query, **params):
        self.queries.append((query, params))
        return SimpleNamespace(single=lambda: _none())


async def _none():
    return None


def _timeline():
    tx = _Tx()
    source = SimpleNamespace(project_id="p1", chapter_id="c1", source_hash="h1")
    return tx, VersionedTimeline(tx, source)


@pytest.mark.asyncio
async def test_same_chapter_second_location_does_not_create_a_second_edge():
    tx, timeline = _timeline()
    await timeline._set_character_location("p1", "周芸", "病房", 11)
    query, params = tx.queries[-1]
    assert "OPTIONAL MATCH (c)-[existing:AT_LOCATION" in query
    assert "CASE WHEN existing IS NULL THEN [1] ELSE [] END" in query
    assert "alternate_locations" in query
    assert params["location"] == "病房" and params["idx"] == 11


@pytest.mark.asyncio
@pytest.mark.external_services
async def test_live_two_locations_in_one_chapter_keep_one_edge():
    uri = os.environ.get("ENTITY_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("requires explicit disposable ENTITY_TEST_NEO4J_URI")
    if uri != "bolt://127.0.0.1:57687":
        pytest.fail("refusing non-test Neo4j")
    auth_raw = os.environ.get("ENTITY_TEST_NEO4J_AUTH", "neo4j:aiwrite_test_only")
    user, _, password = auth_raw.partition(":")

    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        try:
            await driver.verify_connectivity()
        except Exception as exc:  # 连不上不是本用例要测的东西
            pytest.skip(f"test Neo4j unreachable: {exc}")
        project_id = f"loc-guard-{uuid4().hex[:8]}"
        async with driver.session() as session:
            await session.run(
                "CREATE CONSTRAINT IF NOT EXISTS FOR ()-[r:AT_LOCATION]-() "
                "REQUIRE (r.project_id, r.character_name, r.chapter_start) IS UNIQUE")
            await session.run("MERGE (c:Character {project_id:$pid,name:$name})",
                              pid=project_id, name="周芸")
            try:
                timeline = VersionedTimeline(_Neo4jTx(session),
                                             SimpleNamespace(project_id=project_id,
                                                             chapter_id="ch11",
                                                             source_hash="h1"))
                await timeline._set_character_location(project_id, "周芸", "医生办公室", 11)
                await timeline._set_character_location(project_id, "周芸", "病房", 11)
                rows = await (await session.run(
                    "MATCH (c:Character {project_id:$pid,name:$name})"
                    "-[r:AT_LOCATION]->(l:Location) "
                    "RETURN l.name AS loc, r.chapter_start AS idx, "
                    "r.alternate_locations AS alt", pid=project_id, name="周芸")).data()
            finally:
                await session.run("MATCH (c:Character {project_id:$pid,name:$name}) "
                                  "DETACH DELETE c", pid=project_id, name="周芸")
                await session.run("MATCH (l:Location {project_id:$pid}) DETACH DELETE l",
                                  pid=project_id)
        assert len(rows) == 1
        assert rows[0]["loc"] == "医生办公室" and rows[0]["idx"] == 11
        assert rows[0]["alt"] == ["病房"]
    finally:
        await driver.close()


class _Neo4jTx:
    def __init__(self, session):
        self.session = session

    async def run(self, query, **params):
        return await self.session.run(query, **params)
