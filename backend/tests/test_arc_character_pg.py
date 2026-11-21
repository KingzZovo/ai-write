"""Real PostgreSQL guard ordering; graph calls are isolated doubles here."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.api import neo4j_settings as api
from app.models.project import Chapter
from app.models.cognition import ChapterCognition
from app.services.chapter_order import lock_chapter_structure
from app.services.entity_sources import resolve_entity_source
from app.services.arc_planning_sources import read_planning_source
from tests.test_arc_planning_pg import planning_pg
from tests.test_pipeline_control import runtime_db
from tests.support.planning_cognition import cognition_values

pytestmark = pytest.mark.external_services


def install_character_writer(r, monkeypatch, *, release=None):
    entered = asyncio.Event()
    async def write(*args):
        entered.set()
        if release is not None: await release.wait()
        r.character_graph.profiles.append(dict(node_id='new-node', id=str(uuid4()), name='林禾',
            profile_json='{"左手":"尚未恢复"}', profile_origin='author_edit'))
        return 'sync-token'
    monkeypatch.setattr('app.services.character_profile_write.write_character_profile', write)
    monkeypatch.setattr('app.services.character_profile_recovery.acknowledge_profiles', AsyncMock())
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', AsyncMock(return_value={}))
    async def save():
        async with r.sessions() as db:
            return await api.upsert_character(str(r.project), api.Neo4jCharacterUpsertRequest(
                name='林禾', profile_json={'左手': '尚未恢复'}, expected_profile_json=None), object(), db)
    return entered, save


@pytest.mark.asyncio
async def test_planning_snapshot_excludes_official_character_edit_until_commit(planning_pg, monkeypatch):
    r = planning_pg
    entered, save = install_character_writer(r, monkeypatch)
    async with r.sessions() as db:
        await lock_chapter_structure(db, r.project)
        before = await read_planning_source(db, r.project, lock=True)
        task = asyncio.create_task(save())
        try:
            await asyncio.sleep(0.1)
            assert not task.done() and not entered.is_set()
            assert (await read_planning_source(db, r.project, lock=True)) == before
        finally:
            await db.commit()
            await asyncio.wait_for(task, 10)
    async with r.sessions() as db:
        after = await read_planning_source(db, r.project)
    assert after['character_context']['profiles'][0]['name'] == '林禾'


@pytest.mark.asyncio
async def test_inflight_character_edit_finishes_before_planning_reads_new_source(planning_pg, monkeypatch):
    r = planning_pg
    release = asyncio.Event()
    entered, save = install_character_writer(r, monkeypatch, release=release)
    writer = asyncio.create_task(save())
    await asyncio.wait_for(entered.wait(), 10)
    async def planning():
        async with r.sessions() as db:
            await lock_chapter_structure(db, r.project)
            source = await read_planning_source(db, r.project, lock=True)
            await db.commit()
            return source
    planner = asyncio.create_task(planning())
    try:
        await asyncio.sleep(0.1)
        assert not planner.done()
    finally:
        release.set()
        await asyncio.wait_for(writer, 10)
    source = await asyncio.wait_for(planner, 10)
    assert source['character_context']['profiles'][0]['profile_json'] == '{"左手":"尚未恢复"}'


@pytest.mark.asyncio
async def test_planning_volume_chapter_locks_exclude_entity_publication_source_lock(planning_pg):
    r = planning_pg
    async with r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(status='completed', content_text='林禾留在船舱。'))
        await db.commit()
        source = await resolve_entity_source(db, r.project, 0, r.chapter)
        await db.execute(insert(ChapterCognition).values(**cognition_values(source, [])))
        await db.commit()
    entered = asyncio.Event()
    async def publication():
        async with r.sessions() as db:
            source = await resolve_entity_source(db, r.project, 0, r.chapter, lock=True)
            assert source is not None
            entered.set()
            await db.commit()
    async with r.sessions() as db:
        await lock_chapter_structure(db, r.project)
        await read_planning_source(db, r.project, lock=True)
        task = asyncio.create_task(publication())
        try:
            await asyncio.sleep(0.1)
            assert not entered.is_set() and not task.done()
        finally:
            await db.commit()
            await asyncio.wait_for(task, 10)
    assert entered.is_set()
