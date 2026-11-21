"""Real Neo4j Cypher and PG body sources; --offline must skip these."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.models.project import Chapter
from app.services.chapter_order import lock_chapter_structure
from app.services.planning_character_sources import read_planning_character_source
from app.services.entity_context import verified_world_snapshot
from app.services import entity_projection as ep
from tests.test_pipeline_control import runtime_db
from tests.test_entity_projection import env, payload, run

pytestmark = pytest.mark.external_services


@pytest.mark.asyncio
async def test_real_graph_profile_state_relationship_and_location_query(env, monkeypatch):
    e = env
    body = '江枫留在酒馆。老余答应借他一床被褥。'
    async with e.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == e.r.chapter).values(content_text=body))
        await db.commit()
    data = payload(body)
    data['new_relationships'] = [{'char1': '老余', 'char2': '江枫', 'type': '借出一床被褥', 'evidence': body}]
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=data))
    await run(e)
    async with e.driver.session() as session:
        await (await session.run('MATCH (c:Character {project_id:$pid,name:"江枫"}) '
            'SET c.profile_origin="author_edit",c.profile_json=$profile',
            pid=str(e.r.project), profile='{"身份":"作者当前设定"}')).consume()
    async with e.r.sessions() as db:
        snapshot = await verified_world_snapshot(db, e.driver, e.r.project, 2, include_profiles=True)
    assert snapshot.profiles[0]['node_id'] and snapshot.profiles[0]['profile_origin'] == 'author_edit'
    assert snapshot.characters[0].status == {'状态': '酒馆'}
    assert snapshot.location_records[0]['location'] == '酒馆'
    assert [(rel.source, rel.target, rel.evidence) for rel in snapshot.relationships] == [('老余', '江枫', body)]
    async with e.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == e.r.chapter).values(content_text='作者刚修改了保存稿。'))
        await db.commit()
        changed = await verified_world_snapshot(db, e.driver, e.r.project, 2, include_profiles=True)
    assert changed.profiles == snapshot.profiles
    assert not changed.characters and not changed.location_records and not changed.relationships


@pytest.mark.asyncio
async def test_real_entity_publisher_waits_for_final_planning_snapshot(env, monkeypatch):
    e = env
    extracted, release_extraction = asyncio.Event(), asyncio.Event()
    async def extract(**kwargs):
        extracted.set()
        await release_extraction.wait()
        return payload()
    async def driver(): yield e.driver
    monkeypatch.setattr(ep, 'extract_entity_changes', extract)
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    # Let the publisher commit its request row first. Starting it under the
    # Project UPDATE lock would block that FK check before reaching extraction.
    task = asyncio.create_task(run(e))
    await asyncio.wait_for(extracted.wait(), 10)
    async with e.r.sessions() as db:
        await lock_chapter_structure(db, e.r.project)
        before = await read_planning_character_source(db, e.r.project, 2, lock=True)
        try:
            release_extraction.set()
            await asyncio.sleep(0.1)
            assert not task.done()
            assert (await read_planning_character_source(db, e.r.project, 2, lock=True)) == before
        finally:
            release_extraction.set()
            await db.commit()
            await asyncio.wait_for(task, 10)
    async with e.r.sessions() as db:
        after = await read_planning_character_source(db, e.r.project, 2)
    assert after['states'][0]['status'] == {'状态': '酒馆'}
