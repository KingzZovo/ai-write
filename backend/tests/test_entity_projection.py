"""Real PG/Neo4j source version, replay and atomic graph replacement tests."""
import asyncio
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from sqlalchemy import select, update, func, MetaData, Table
from app.models.project import Chapter, Volume
from app.models.entity_extraction import ChapterEntityExtraction as Record, entity_projection_tokens
from app.services import entity_projection as ep
from app.services.entity_sources import resolve_entity_source, digest
from app.services.entity_graph_projection import project_entity_payload, ProjectionSuperseded, VersionedTimeline
from app.services.entity_timeline import EntityTimelineService
from app.services.chapter_entity_extraction import FIELDS
from app.services.relationship_identity import relationship_record_id
from tests.test_pipeline_control import runtime_db

async def prepare(r):
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: entity_projection_tokens.create(sync, checkfirst=True))
        await conn.run_sync(lambda sync: Record.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = '江枫留在酒馆。'
        ch.status = 'completed'
        ch.global_idx = 1
        await db.commit()
        return ch.volume_id

async def source(r, chapter=None):
    async with r.sessions() as db:
        return await resolve_entity_source(db, r.project, 1, chapter or r.chapter)


def payload(body='江枫留在酒馆。', state='酒馆', location=True):
    data = {key: [] for key in FIELDS}
    data['new_characters'] = [{'name':'江枫', 'state':{'状态':state}, 'evidence':body}]
    if location: data['location_changes'] = [{'character':'江枫','location':state,'evidence':body}]
    return data

@pytest_asyncio.fixture
async def env(runtime_db):
    r = runtime_db
    await prepare(r)
    uri = os.environ.get('ENTITY_TEST_NEO4J_URI')
    if not uri: pytest.skip('requires explicit disposable ENTITY_TEST_NEO4J_URI')
    if uri != 'bolt://127.0.0.1:57687': pytest.fail('refusing non-test Neo4j')
    driver = AsyncGraphDatabase.driver(uri, auth=('neo4j','aiwrite_test_only'))
    await driver.verify_connectivity()
    await EntityTimelineService(driver, strict_writes=True).initialize_graph(str(r.project))
    try:
        yield SimpleNamespace(r=r, driver=driver, materialize=AsyncMock(return_value={}), cards=AsyncMock(return_value={}))
    finally:
        async with driver.session() as session:
            await session.run('MATCH (c:Character {project_id:$pid}) OPTIONAL MATCH (c)-[:HAS_STATE]->(s:CharacterState) DETACH DELETE s,c',pid=str(r.project))
            await session.run('MATCH (n {project_id:$pid}) DETACH DELETE n',pid=str(r.project))
        await driver.close()

async def run(e, src=None):
    return await ep.run_entity_projection(source=src or await source(e.r), sessions=e.r.sessions,
        engine=e.r.engine, driver=e.driver, caller='test', materialize=e.materialize, memory_cards=e.cards)

@pytest.mark.asyncio
@pytest.mark.parametrize('case',['valid','foreign','draft','unknown','ambiguous_local','duplicate_global'])
async def test_resolver_does_not_guess_identity_or_chronology(runtime_db, case):
    r = runtime_db
    vid = await prepare(r)
    async with r.sessions() as db:
        if case == 'draft': await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(status='draft'))
        if case == 'unknown': await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(global_idx=None))
        if case in ('ambiguous_local','duplicate_global'):
            v2 = Volume(project_id=r.project, volume_idx=2, title='第二卷'); db.add(v2); await db.flush()
            db.add(Chapter(volume_id=v2.id, chapter_idx=1, global_idx=1 if case=='duplicate_global' else 2, title='同号章'))
        await db.commit()
        result = await resolve_entity_source(db, uuid4() if case=='foreign' else r.project, 1,
                                             None if case=='ambiguous_local' else r.chapter)
        assert (result is not None) == (case == 'valid')

@pytest.mark.asyncio
async def test_same_source_reuses_payload_and_completed_projection(env, monkeypatch):
    generate = AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    assert (await run(env))['status'] == 'ok'
    assert (await run(env))['reason'] == 'already_completed'
    generate.assert_awaited_once(); env.materialize.assert_awaited_once(); env.cards.assert_awaited_once()
    async with env.r.sessions() as db:
        row = await db.scalar(select(Record))
        assert row.status == 'completed' and row.payload_hash == digest([row.source_hash,row.payload_json])

@pytest.mark.asyncio
async def test_new_source_replaces_owned_facts_including_removed_location(env, monkeypatch):
    generate = AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    old = await source(env.r); await run(env,old)
    async with env.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='江枫离开酒馆。')); await db.commit()
    generate.return_value=payload('江枫离开酒馆。','离开',False)
    new = await source(env.r)
    assert new.source_hash != old.source_hash
    assert (await run(env,new))['status']=='ok'
    async with env.driver.session() as session:
        row = await (await session.run('MATCH (:Character {project_id:$pid})-[r:AT_LOCATION]->() RETURN count(r) AS n',pid=str(env.r.project))).single()
        assert row['n']==0
    state = await EntityTimelineService(env.driver).get_character_state_at(str(env.r.project),'江枫',1)
    assert state.status=={'状态':'离开'}

@pytest.mark.asyncio
async def test_edit_during_model_io_discards_payload_before_graph(env, monkeypatch):
    async def generate(**kwargs):
        async with env.r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='改稿。')); await db.commit()
        return payload()
    monkeypatch.setattr(ep,'extract_entity_changes',generate)
    assert (await run(env))['reason']=='stale_source'
    env.materialize.assert_not_awaited()
    async with env.driver.session() as session:
        row=await (await session.run('MATCH (n {project_id:$pid}) RETURN count(n) AS n',pid=str(env.r.project))).single()
        assert row['n']==0

@pytest.mark.asyncio
async def test_projection_failure_reuses_durable_payload(env,monkeypatch):
    generate=AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    env.materialize.side_effect=[RuntimeError('PG read model failed'),{}]
    with pytest.raises(RuntimeError): await run(env)
    async with env.r.sessions() as db:
        row=await db.scalar(select(Record)); assert row.status=='failed' and row.payload_json==payload()
    await run(env)
    generate.assert_awaited_once(); assert env.materialize.await_count==2
    async with env.r.sessions() as db: assert (await db.scalar(select(Record))).status=='completed'

@pytest.mark.asyncio
async def test_project_lease_blocks_duplicate_model_calls(env,monkeypatch):
    started,release=asyncio.Event(),asyncio.Event()
    async def generate(**kwargs): started.set(); await release.wait(); return payload()
    generate_mock=AsyncMock(side_effect=generate); monkeypatch.setattr(ep,'extract_entity_changes',generate_mock)
    job=asyncio.create_task(run(env))
    await asyncio.wait_for(started.wait(),10)
    try:
        with pytest.raises(ep.EntityProjectionBusy): await run(env)
    finally: release.set()
    await job; generate_mock.assert_awaited_once()

@pytest.mark.asyncio
async def test_graph_fencing_rejects_late_token(env):
    src=await source(env.r); data=payload(); receipt=digest([src.source_hash,data])
    await project_entity_payload(env.driver,src,data,receipt,100)
    with pytest.raises(ProjectionSuperseded): await project_entity_payload(env.driver,src,data,receipt,99)

@pytest.mark.asyncio
async def test_graph_failure_rolls_back_all_facts_and_marker(env,monkeypatch):
    async def fail(*args,**kwargs): raise RuntimeError('late graph failure')
    monkeypatch.setattr(VersionedTimeline,'_set_character_location',fail)
    src=await source(env.r); data=payload()
    with pytest.raises(RuntimeError): await project_entity_payload(env.driver,src,data,digest([src.source_hash,data]),1)
    async with env.driver.session() as session:
        row=await (await session.run('MATCH (n {project_id:$pid}) RETURN count(n) AS n',pid=str(env.r.project))).single()
        assert row['n']==0

@pytest.mark.asyncio
async def test_revising_earlier_chapter_preserves_later_timeline(env,monkeypatch):
    generate=AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    await run(env)
    async with env.r.sessions() as db:
        first=await db.get(Chapter,env.r.chapter)
        later=Chapter(volume_id=first.volume_id, chapter_idx=2, global_idx=2, title='后章',status='completed',content_text='江枫到了码头。')
        db.add(later); await db.commit(); later_id=later.id
    generate.return_value=payload('江枫到了码头。','码头')
    await run(env,await source(env.r,later_id))
    async with env.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='江枫在客栈。')); await db.commit()
    generate.return_value=payload('江枫在客栈。','客栈')
    await run(env)
    service=EntityTimelineService(env.driver)
    assert (await service.get_character_state_at(str(env.r.project),'江枫',1)).status['状态']=='客栈'
    assert (await service.get_character_state_at(str(env.r.project),'江枫',2)).status['状态']=='码头'

@pytest.mark.asyncio
async def test_replay_selects_retained_payload_but_not_exhausted_model_failure(runtime_db,monkeypatch):
    r=runtime_db; await prepare(r); src=await source(r)
    rid=await ep.ensure_record(r.sessions,src)
    dispatch=AsyncMock()  # dispatcher is synchronous
    calls=[]
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction',lambda **kwargs: calls.append(kwargs) or True)
    async with r.sessions() as db:
        await db.execute(update(Record).where(Record.id==rid).values(status='failed',updated_at=ep.now()-timedelta(minutes=2)))
        await db.commit()
    assert (await ep.replay_pending_extractions(r.sessions))['queued']==0
    async with r.sessions() as db:
        await db.execute(update(Record).where(Record.id==rid).values(
            payload_hash=digest([src.source_hash,payload()]),payload_json=payload()))
        await db.commit()
    assert (await ep.replay_pending_extractions(r.sessions))['queued']==1
    assert str(calls[0]['chapter_id'])==str(r.chapter)

@pytest.mark.asyncio
async def test_real_read_projections_follow_revision_and_removed_facts(env,monkeypatch):
    from app.models import Base
    from app.models.project import CharacterState, CharacterLocation
    from app.tasks.entity_tasks import _materialize_entities_to_postgres, _upsert_memory_cards
    async with env.r.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', env.r.sessions)
    env.materialize=_materialize_entities_to_postgres
    env.cards=_upsert_memory_cards
    generate=AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    await run(env)
    async with env.r.sessions() as db:
        assert (await db.scalar(select(CharacterState))).status_json == {'状态':'酒馆'}
        assert await db.scalar(select(func.count()).select_from(CharacterLocation)) == 1
        await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='江枫离开酒馆。')); await db.commit()
    generate.return_value=payload('江枫离开酒馆。','离开',False)
    await run(env)
    async with env.r.sessions() as db:
        assert (await db.scalar(select(CharacterState))).status_json == {'状态':'离开'}
        assert await db.scalar(select(func.count()).select_from(CharacterLocation)) == 0

@pytest.mark.asyncio
async def test_parallel_relationships_survive_an_explicitly_scoped_change(env):
    src=await source(env.r)
    data={key:[] for key in FIELDS}
    data['new_relationships']=[{'char1':'江枫','char2':'店主','type':kind,'evidence':src.content} for kind in ['同事','朋友']]
    await project_entity_payload(env.driver,src,data,digest([src.source_hash,data]),1)
    service=EntityTimelineService(env.driver)
    assert len(await service.get_relationships_at(str(env.r.project),1))==2
    async with env.r.sessions() as db:
        ch=await db.get(Chapter,env.r.chapter)
        next_ch=Chapter(volume_id=ch.volume_id, chapter_idx=2,global_idx=2,status='completed',title='后章',content_text='江枫与店主反目。')
        db.add(next_ch); await db.commit(); next_id=next_ch.id
    newer=await source(env.r,next_id)
    changed={key:[] for key in FIELDS}
    changed['changed_relationships']=[{'char1':'江枫','char2':'店主','type':'敌对','evidence':newer.content,
        'replaces':[relationship_record_id(src.project_id,'江枫','店主','朋友',src.chapter_id,src.source_hash)]}]
    await project_entity_payload(env.driver,newer,changed,digest([newer.source_hash,changed]),2)
    assert len(await service.get_relationships_at(str(env.r.project),1))==2
    assert {rel.rel_type for rel in await service.get_relationships_at(str(env.r.project),2)}=={'同事','敌对'}


@pytest.mark.asyncio
async def test_real_v2_unscoped_relationship_replay_restores_parallel_intervals(env):
    from app.services.entity_context import verified_world_snapshot
    src = await source(env.r)
    data = {key: [] for key in FIELDS}
    data['new_relationships'] = [dict(char1='江枫', char2='店主', type=kind, evidence=src.content)
                                 for kind in ('同事', '朋友')]
    await project_entity_payload(env.driver, src, data, digest([src.source_hash, data]), 1)
    async with env.r.sessions() as db:
        ch = await db.get(Chapter, env.r.chapter)
        later = Chapter(volume_id=ch.volume_id, chapter_idx=2, global_idx=2, status='completed',
                        title='后章', content_text='江枫与店主反目。')
        db.add(later); await db.commit(); next_id = later.id
    newer = await source(env.r, next_id)
    changed = {key: [] for key in FIELDS}
    changed['changed_relationships'] = [dict(char1='江枫', char2='店主', type='敌对', evidence=newer.content)]
    await project_entity_payload(env.driver, newer, changed, digest([newer.source_hash, changed]), 2)
    async with env.driver.session() as session:
        await session.run('MATCH (:Character {project_id:$pid})-[r:RELATES_TO]->() '
            'REMOVE r.record_id,r.evidence,r.replaces '
            'WITH r WHERE r.chapter_start=1 SET r.chapter_end=1', pid=str(env.r.project))
        await session.run('MATCH (m:ChapterEntityProjection {project_id:$pid,chapter_id:$cid}) '
            'SET m.schema_version=2', pid=str(env.r.project), cid=newer.chapter_id)
    assert await project_entity_payload(env.driver, newer, changed, digest([newer.source_hash, changed]), 3)
    assert {rel.rel_type for rel in await EntityTimelineService(env.driver).get_relationships_at(str(env.r.project), 2)} == {'同事','朋友','敌对'}
    async with env.r.sessions() as db:
        snapshot = await verified_world_snapshot(db, env.driver, env.r.project, 3)
        assert all(rel.scope_pending for rel in snapshot.relationships if rel.rel_type != '敌对')


@pytest.mark.asyncio
async def test_real_author_edit_of_closing_event_restores_original_interval(env):
    from app.services.relationship_graph_write import apply_relationship_graph_write, read_relationship_snapshot
    from app.services.relationship_write_contract import make_relationship_command
    src = await source(env.r)
    data = {key: [] for key in FIELDS}
    data['new_relationships'] = [dict(char1='江枫', char2='店主', type='朋友', evidence=src.content)]
    await project_entity_payload(env.driver, src, data, digest([src.source_hash, data]), 1)
    async with env.r.sessions() as db:
        ch = await db.get(Chapter, env.r.chapter)
        later = Chapter(volume_id=ch.volume_id, chapter_idx=2, global_idx=2, status='completed',
                        title='后章', content_text='江枫与店主反目。')
        db.add(later); await db.commit(); next_id = later.id
    newer = await source(env.r, next_id)
    changed = {key: [] for key in FIELDS}
    rid = relationship_record_id(src.project_id, '江枫', '店主', '朋友', src.chapter_id, src.source_hash)
    changed['changed_relationships'] = [dict(char1='江枫', char2='店主', type='敌对', evidence=newer.content, replaces=[rid])]
    await project_entity_payload(env.driver, newer, changed, digest([newer.source_hash, changed]), 2)
    before = await read_relationship_snapshot(env.driver, env.r.project, '江枫', '店主', '敌对')
    command = make_relationship_command(uuid4(), env.r.project, '江枫', '店主', '敌对', 'delete', before['version'], {})
    await apply_relationship_graph_write(env.driver, command)
    assert {rel.rel_type for rel in await EntityTimelineService(env.driver).get_relationships_at(str(env.r.project), 2)} == {'朋友'}

@pytest.mark.asyncio
async def test_interval_repair_does_not_reopen_manually_ended_state(env):
    async with env.driver.session() as session:
        await session.run('CREATE (c:Character {project_id:$pid,name:"江枫"})-[:HAS_STATE]->(:CharacterState {id:"manual",chapter_start:0,chapter_end:0,status_json:"{}"})',pid=str(env.r.project))
    src=await source(env.r); data=payload()
    await project_entity_payload(env.driver,src,data,digest([src.source_hash,data]),1)
    empty={key:[] for key in FIELDS}
    await project_entity_payload(env.driver,src,empty,digest([src.source_hash,empty]),2)
    assert await EntityTimelineService(env.driver).get_character_state_at(str(env.r.project),'江枫',1) is None

@pytest.mark.asyncio
async def test_reverting_body_reuses_payload_with_a_new_fencing_token(env,monkeypatch):
    generate=AsyncMock(return_value=payload()); monkeypatch.setattr(ep,'extract_entity_changes',generate)
    first=await run(env)
    async with env.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='江枫在客栈。')); await db.commit()
    generate.return_value=payload('江枫在客栈。','客栈')
    second=await run(env)
    async with env.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id==env.r.chapter).values(content_text='江枫留在酒馆。')); await db.commit()
    reverted=await run(env)
    assert first['projection_token']<second['projection_token']<reverted['projection_token']
    assert generate.await_count==2
    assert (await EntityTimelineService(env.driver).get_character_state_at(str(env.r.project),'江枫',1)).status['状态']=='酒馆'

@pytest.mark.asyncio
async def test_cancelled_worker_releases_connection_lease(env,monkeypatch):
    started=asyncio.Event()
    async def blocked(**kwargs): started.set(); await asyncio.Event().wait()
    monkeypatch.setattr(ep,'extract_entity_changes',blocked)
    worker=asyncio.create_task(run(env)); await asyncio.wait_for(started.wait(),10)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError): await worker
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=payload()))
    assert (await run(env))['status']=='ok'

@pytest.mark.asyncio
async def test_replay_retires_old_source_and_keeps_new_pending_on_broker_failure(runtime_db,monkeypatch):
    r=runtime_db; await prepare(r); old=await source(r); rid=await ep.ensure_record(r.sessions,old)
    async with r.sessions() as db:
        await db.execute(update(Record).where(Record.id==rid).values(updated_at=ep.now()-timedelta(minutes=2)))
        await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(content_text='江枫在客栈。')); await db.commit()
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction',lambda **kwargs: False)
    assert (await ep.replay_pending_extractions(r.sessions))['queued']==0
    async with r.sessions() as db:
        rows=(await db.execute(select(Record))).scalars().all()
        assert {row.status for row in rows}=={'pending','obsolete'}
        assert next(row for row in rows if row.status=='pending').source_hash != old.source_hash

@pytest.mark.asyncio
async def test_real_materialization_commit_failure_is_not_completed(env,monkeypatch):
    from contextlib import asynccontextmanager
    from app.models import Base
    from app.tasks.entity_tasks import _materialize_entities_to_postgres
    async with env.r.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    @asynccontextmanager
    async def broken():
        async with env.r.sessions() as db:
            db.commit=AsyncMock(side_effect=RuntimeError('commit rejected'))
            yield db
    monkeypatch.setattr('app.db.session.async_session_factory',broken)
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=payload()))
    env.materialize=_materialize_entities_to_postgres
    with pytest.raises(RuntimeError,match='commit rejected'): await run(env)
    async with env.r.sessions() as db:
        row=await db.scalar(select(Record))
        assert row.status=='failed' and row.payload_hash
    env.cards.assert_not_awaited()

@pytest.mark.asyncio
async def test_migration_roundtrip_keeps_author_chapter(runtime_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r=runtime_db; await prepare(r)
    path=Path(__file__).resolve().parents[1]/'alembic/versions/a1001924_entity_extraction_versions.py'
    spec=importlib.util.spec_from_file_location('entity_migration',path)
    migration=importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    async with r.engine.begin() as conn:
        def roundtrip(sync):
            migration.op=Operations(MigrationContext.configure(sync))
            migration.downgrade(); migration.upgrade()
            return Table('chapter_entity_extractions', MetaData(), autoload_with=sync)
        legacy_table = await conn.run_sync(roundtrip)
    src=await source(r); rid=uuid4()
    async with r.sessions() as db:
        # Stop deliberately at 1924: current ORM defaults include later stage
        # columns. Exercise the historical schema with its reflected table.
        await db.execute(legacy_table.insert().values(id=rid, project_id=r.project, chapter_id=r.chapter,
            source_hash=src.source_hash, source_json=src.receipt, status='pending', created_at=ep.now(), updated_at=ep.now()))
        await db.commit()
        assert (await db.get(Chapter,r.chapter)).content_text=='江枫留在酒馆。'
        # This test intentionally stops at a1001924, before later model columns.
        row=(await db.execute(select(Record.status, Record.payload_json, Record.projection_token)
                              .where(Record.id==rid))).one()
        assert row.status=='pending' and row.payload_json is None and row.projection_token>0


@pytest.mark.asyncio
async def test_real_scope_checkpoint_releases_author_rows_before_model_and_rejects_changed_source(env, monkeypatch):
    from app.services import relationship_scope_delivery as delivery
    from app.services.relationship_scope import SYSTEM_PROMPT
    first = await source(env.r)
    original = {key: [] for key in FIELDS}
    original['new_relationships'] = [dict(char1='江枫', char2='店主', type=kind, evidence=first.content)
                                    for kind in ('朋友', '同事')]
    await project_entity_payload(env.driver, first, original, digest([first.source_hash, original]), 1)
    async with env.r.sessions() as db:
        ch = await db.get(Chapter, env.r.chapter)
        later = Chapter(volume_id=ch.volume_id, chapter_idx=2, global_idx=2, title='后章',
                        status='completed', content_text='江枫与店主绝交，公事照旧。')
        db.add(later); await db.commit(); later_id = later.id
    current = await source(env.r, later_id)
    changes = {key: [] for key in FIELDS}
    changes['changed_relationships'] = [dict(char1='江枫', char2='店主', type='绝交', evidence=current.content)]
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=changes))
    monkeypatch.setattr('app.services.prompt_cache.get_snapshot',
        AsyncMock(return_value=SimpleNamespace(system_prompt=SYSTEM_PROMPT, endpoint_id=uuid4())))

    async def review(src, event, batch, db):
        # A separate PG connection must be able to commit an author revision
        # during model IO; SQLite cannot establish this lock behavior.
        async def author_edit():
            async with env.r.sessions() as author_db:
                await author_db.execute(update(Chapter).where(Chapter.id == later_id).values(content_text='作者重新写了后章。'))
                await author_db.commit()
        await asyncio.wait_for(author_edit(), 5)
        return [dict(record_id=row['record_id'], ended=row['type'] == '朋友',
                     evidence=src.content if row['type'] == '朋友' else '') for row in batch['candidates']]

    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=review))
    assert (await run(env, current))['reason'] == 'stale_source'
    env.materialize.assert_not_awaited()
    async with env.r.sessions() as db:
        row = await db.scalar(select(Record).where(Record.chapter_id == later_id))
        assert row.status == 'obsolete' and row.payload_stage == 'scope_pending'
        assert row.scope_json['batches'][0]['reviews'] is None
    assert {row.rel_type for row in await EntityTimelineService(env.driver).get_relationships_at(str(env.r.project), 2)} == {'朋友', '同事'}
