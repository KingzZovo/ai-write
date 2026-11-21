"""Real graph/PG chapter deltas prevent source laundering across chapters."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, update
from app.models.project import Chapter, CharacterState
from app.models.entity_extraction import ChapterEntityExtraction as Record
from app.services import entity_projection as ep
from app.services.entity_context import verified_world_snapshot, verified_character_states
from app.services.context_pack import ContextPackBuilder, ContextPack, CharacterCard
from app.services.chapter_entity_extraction import FIELDS
from tests.test_entity_invalidation import clean_env, change
from tests.test_entity_projection import env, run, source
from tests.test_pipeline_control import runtime_db

async def two_chapters(e,monkeypatch,second_changes=None):
    body='江枫修为筑基，心中焦虑。';await change(e,content_text=body)
    data={key:[] for key in FIELDS}
    data['new_characters']=[dict(name='江枫',state={'能力等级':'筑基','情绪':'焦虑'},evidence=body)]
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=data));await run(e)
    async with e.r.sessions() as db:
        first=await db.get(Chapter,e.r.chapter)
        second=Chapter(volume_id=first.volume_id,chapter_idx=2,global_idx=2,title='第二章',
                       content_text='江枫心情平静。',status='completed')
        db.add(second);await db.commit();second_id=second.id
    second_data={key:[] for key in FIELDS}
    second_data['state_changes']=[dict(name='江枫',changes=second_changes or {'情绪':'平静'},evidence=second.content_text)]
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=second_data))
    await run(e,await source(e.r,second_id))
    return second_id

@pytest.mark.asyncio
async def test_new_chapter_stores_only_its_delta_and_reads_fold_verified_history(clean_env,monkeypatch):
    e=clean_env;second=await two_chapters(e,monkeypatch)
    async with e.r.sessions() as db:
        row=await db.scalar(select(CharacterState).where(CharacterState.chapter_start==2))
        assert row.status_json=={'情绪':'平静'} and row.source_mode=='chapter_delta'
        assert str(row.source_chapter_id)==str(second)
        pg=await verified_character_states(db,e.r.project,3)
        graph=(await verified_world_snapshot(db,e.driver,e.r.project,3)).characters[0]
        assert graph.status==pg['江枫']['status']=={'能力等级':'筑基','情绪':'平静'}
        assert str(graph.field_sources['能力等级']['source_chapter_id'])==str(e.r.chapter)
        assert str(graph.field_sources['情绪']['source_chapter_id'])==str(second)

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['body','draft','delete'])
async def test_invalidating_previous_source_does_not_survive_in_later_chapter(clean_env,monkeypatch,mutation):
    from sqlalchemy import delete
    e=clean_env;await two_chapters(e,monkeypatch)
    if mutation=='body':await change(e,content_text='江枫仍在炼气。')
    elif mutation=='draft':await change(e,status='draft')
    else:
        async with e.r.sessions() as db:
            await db.execute(delete(Chapter).where(Chapter.id==e.r.chapter));await db.commit()
    async with e.r.sessions() as db:
        pg=await verified_character_states(db,e.r.project,3)
        graph=(await verified_world_snapshot(db,e.driver,e.r.project,3)).characters[0]
        assert graph.status==pg['江枫']['status']=={'情绪':'平静'}
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._overlay_character_states(pack,str(e.r.project),3)
        assert pack.character_cards[0].power_level=='' and pack.character_cards[0].mental_state=='平静'

@pytest.mark.asyncio
async def test_explicit_unknown_does_not_revive_old_field(clean_env,monkeypatch):
    e=clean_env;await two_chapters(e,monkeypatch,{'能力等级':None,'情绪':'平静'})
    async with e.r.sessions() as db:
        assert (await verified_character_states(db,e.r.project,3))['江枫']['status']['能力等级'] is None
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫',power_level='筑基')]
        await ContextPackBuilder(db=db)._overlay_character_states(pack,str(e.r.project),3)
        assert pack.character_cards[0].power_level==''

@pytest.mark.asyncio
async def test_same_chapter_new_character_and_change_can_merge_locally(clean_env,monkeypatch):
    e=clean_env;src=await source(e.r)
    data={key:[] for key in FIELDS}
    data['new_characters']=[dict(name='江枫',state={'能力等级':'筑基','情绪':'焦虑'},evidence=src.content)]
    data['state_changes']=[dict(name='江枫',changes={'情绪':'平静'},evidence=src.content)]
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=data));await run(e)
    async with e.r.sessions() as db:
        assert (await db.scalar(select(CharacterState))).status_json=={'能力等级':'筑基','情绪':'平静'}

@pytest.mark.asyncio
async def test_old_merged_snapshot_hidden_then_rebuilt_without_model(clean_env,monkeypatch):
    e=clean_env;second=await two_chapters(e,monkeypatch)
    # Recreate an old-format graph/PG snapshot with an inherited wrong field.
    async with e.driver.session() as session:
        await session.run('MATCH (m:ChapterEntityProjection {project_id:$pid,chapter_id:$cid}) SET m.schema_version=1',pid=str(e.r.project),cid=str(second))
        await session.run('MATCH (s:CharacterState {source_chapter_id:$cid}) SET s.source_mode=null,s.status_json=$status',cid=str(second),status=json.dumps({'能力等级':'伪造旧值','情绪':'平静'},ensure_ascii=False))
    async with e.r.sessions() as db:
        await db.execute(update(CharacterState).where(CharacterState.source_chapter_id==second).values(source_mode=None,status_json={'能力等级':'伪造旧值','情绪':'平静'}))
        await db.execute(update(Record).where(Record.chapter_id==second).values(projection_version=1,updated_at=ep.now()-timedelta(minutes=2)))
        await db.commit()
        assert '伪造旧值' not in str(await verified_character_states(db,e.r.project,3))
        assert '伪造旧值' not in str(await verified_world_snapshot(db,e.driver,e.r.project,3))
    calls=[]
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction',lambda **kw:calls.append(kw) or True)
    assert (await ep.replay_pending_extractions(e.r.sessions))['queued']==1
    assert str(calls[0]['chapter_id'])==str(second)
    no_model=AsyncMock(side_effect=AssertionError('upgrade must reuse payload'))
    monkeypatch.setattr(ep,'extract_entity_changes',no_model)
    await run(e,await source(e.r,second));no_model.assert_not_awaited()
    async with e.r.sessions() as db:
        assert (await db.scalar(select(Record).where(Record.chapter_id==second))).projection_version==ep.ENTITY_PROJECTION_VERSION
        assert (await db.scalar(select(CharacterState).where(CharacterState.chapter_start==2))).status_json=={'情绪':'平静'}
        assert (await verified_character_states(db,e.r.project,3))['江枫']['status']=={'能力等级':'筑基','情绪':'平静'}

@pytest.mark.asyncio
async def test_new_graph_old_pg_version_still_refreshes_projection(clean_env,monkeypatch):
    e=clean_env;await two_chapters(e,monkeypatch)
    async with e.r.sessions() as db:
        await db.execute(update(Record).where(Record.chapter_id==e.r.chapter).values(projection_version=1))
        await db.execute(update(CharacterState).where(CharacterState.source_chapter_id==e.r.chapter).values(source_mode=None))
        await db.commit()
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(side_effect=AssertionError('must reuse payload')))
    assert (await run(e))['reason']=='replayed'
    async with e.r.sessions() as db:
        assert (await db.scalar(select(CharacterState).where(CharacterState.source_chapter_id==e.r.chapter))).source_mode=='chapter_delta'

@pytest.mark.asyncio
async def test_corrupted_legacy_payload_does_not_trigger_automatic_model_calls(clean_env,monkeypatch):
    e=clean_env;await run(e)
    async with e.r.sessions() as db:
        await db.execute(update(Record).values(projection_version=1,payload_hash='corrupted',updated_at=ep.now()-timedelta(minutes=2)))
        await db.commit()
    calls=[];monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction',lambda **kw:calls.append(kw) or True)
    assert (await ep.replay_pending_extractions(e.r.sessions))['queued']==0
    assert calls==[]
    async with e.r.sessions() as db:
        row=await db.scalar(select(Record))
        assert row.status=='failed' and row.last_error=='InvalidRetainedPayload' and row.payload_hash is None
    assert (await ep.replay_pending_extractions(e.r.sessions))['examined']==0

@pytest.mark.asyncio
async def test_delta_migration_roundtrip_preserves_author_chapter(clean_env):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    e=clean_env
    spec=importlib.util.spec_from_file_location('delta_migration','/root/ai-write/backend/alembic/versions/a1001928_character_state_deltas.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    async with e.r.engine.begin() as conn:
        def cycle(sync):
            module.op=Operations(MigrationContext.configure(sync));module.downgrade();module.upgrade()
        await conn.run_sync(cycle)
    await run(e)
    async with e.r.sessions() as db:
        assert (await db.get(Chapter,e.r.chapter)).content_text=='江枫留在酒馆。'
        assert (await db.scalar(select(Record))).projection_version==ep.ENTITY_PROJECTION_VERSION
