"""Source verification at actual generation context boundaries, before cleanup."""
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from sqlalchemy import delete, select, update
from app.models.project import Chapter, Volume, CharacterState, CharacterLocation, CharacterMemoryCard, CharacterAppearance
from app.services.entity_sources import filter_current_entity_rows
from app.services.entity_context import verified_world_snapshot
from app.services.context_pack import ContextPack, ContextPackBuilder, CharacterCard
from app.services.memory import HierarchicalMemory
from app.services import entity_projection as ep
from tests.test_entity_invalidation import clean_env, change
from tests.test_entity_projection import env, source, payload, run
from tests.test_pipeline_control import runtime_db

async def candidate(e):
    src=await source(e.r)
    return dict(source_chapter_id=src.chapter_id,source_hash=src.source_hash,global_idx=src.global_idx,
                excerpt=src.content)

@pytest.mark.asyncio
@pytest.mark.parametrize('change_kind',['body','title','draft','delete','move','reorder','duplicate','unknown','tamper','current','valid'])
async def test_source_filter_rejects_unverified_facts_without_waiting_for_cleanup(clean_env,change_kind):
    e=clean_env;row=await candidate(e)
    async with e.r.sessions() as db:
        ch=await db.get(Chapter,e.r.chapter)
        if change_kind=='body': ch.content_text='另一个版本。'
        if change_kind=='title': ch.title='新标题'
        if change_kind=='draft': ch.status='draft'
        if change_kind=='delete': await db.execute(delete(Chapter).where(Chapter.id==e.r.chapter))
        if change_kind=='reorder': await db.execute(update(Volume).where(Volume.id==ch.volume_id).values(volume_idx=2))
        if change_kind=='move':
            v=Volume(project_id=e.r.project,title='新卷',volume_idx=2);db.add(v);await db.flush();ch.volume_id=v.id
        if change_kind=='duplicate': db.add(Chapter(volume_id=ch.volume_id,chapter_idx=2,global_idx=1,title='冲突章'))
        await db.commit()
        if change_kind=='unknown': row['source_chapter_id']=None
        if change_kind=='tamper': row['excerpt']='杜撰内容。'
        valid=await filter_current_entity_rows(db,e.r.project,[row],before_global_idx=1 if change_kind=='current' else 2)
        assert bool(valid)==(change_kind=='valid')

@pytest.mark.asyncio
async def test_source_checks_do_not_flush_unrelated_author_changes(clean_env):
    e=clean_env;row=await candidate(e)
    async with e.r.sessions() as db:
        ch=await db.get(Chapter,e.r.chapter);ch.summary='未提交摘要'
        db.flush=AsyncMock(side_effect=AssertionError('read must not flush'))
        assert await filter_current_entity_rows(db,e.r.project,[row],before_global_idx=2)
        db.flush.assert_not_awaited()

@pytest.mark.asyncio
async def test_graph_and_pg_state_overlay_reject_stale_and_current_chapter(clean_env):
    e=clean_env;await run(e)
    async with e.r.sessions() as db:
        state=await db.scalar(select(CharacterState));loc=await db.scalar(select(CharacterLocation))
        assert str(state.source_chapter_id)==str(e.r.chapter) and state.source_hash
        assert loc.source_hash==state.source_hash
        before=await verified_world_snapshot(db,e.driver,e.r.project,2)
        assert before.characters[0].status=={'状态':'酒馆'} and before.locations==['酒馆']
        assert not (await verified_world_snapshot(db,e.driver,e.r.project,1)).characters
        builder=ContextPackBuilder(db=db)
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await builder._overlay_character_states(pack,str(e.r.project),2)
        assert pack.character_cards[0].chapter_state=={'状态':'酒馆'}
        assert pack.character_cards[0].mental_state==''
    await change(e,content_text='江枫离开了。')
    async with e.r.sessions() as db:
        # Graph and PG still contain the previous projection; no invalidation ran.
        assert await db.scalar(select(CharacterState)) is not None
        after=await verified_world_snapshot(db,e.driver,e.r.project,2)
        assert not after.characters and not after.locations
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._overlay_character_states(pack,str(e.r.project),2)
        assert pack.character_cards[0].mental_state==''
        assert await HierarchicalMemory(db,e.driver)._gather_entity_states(str(e.r.project),2)==''

@pytest.mark.asyncio
async def test_returning_cards_need_matching_source_and_verbatim_excerpt(clean_env):
    e=clean_env;await run(e)
    async with e.r.sessions() as db:
        db.add(CharacterAppearance(project_id=e.r.project,character_name='江枫',first_seen_chapter=1,last_seen_chapter=1,appearance_count=1))
        await db.commit()
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(e.r.project),30)
        assert '江枫留在酒馆。' in pack.returning_character_drilldown
        await db.execute(update(CharacterMemoryCard).values(excerpt='江枫去了皇宫。'));await db.commit()
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(e.r.project),30)
        assert not pack.returning_character_drilldown

@pytest.mark.asyncio
async def test_old_memory_cards_hidden_after_edit_before_background_cleanup(clean_env):
    e=clean_env;await run(e)
    async with e.r.sessions() as db:
        db.add(CharacterAppearance(project_id=e.r.project,character_name='江枫',last_seen_chapter=1));await db.commit()
    await change(e,status='draft')
    async with e.r.sessions() as db:
        assert await db.scalar(select(CharacterMemoryCard)) is not None
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(e.r.project),30)
        assert not pack.returning_character_drilldown

@pytest.mark.asyncio
async def test_graph_relationships_exclude_current_and_stale_sources(clean_env,monkeypatch):
    e=clean_env;body='江枫与店主结交。';await change(e,content_text=body)
    data=payload(body);data['new_relationships']=[dict(char1='江枫',char2='店主',type='朋友',evidence=body)]
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=data));await run(e)
    async with e.r.sessions() as db:
        snapshot=await verified_world_snapshot(db,e.driver,e.r.project,2)
        assert snapshot.relationships[0].rel_type=='朋友'
        assert not (await verified_world_snapshot(db,e.driver,e.r.project,1)).relationships
    await change(e,status='draft')
    async with e.r.sessions() as db:
        assert not (await verified_world_snapshot(db,e.driver,e.r.project,2)).relationships

@pytest.mark.asyncio
async def test_actual_fact_builder_does_not_restore_stale_location_from_either_store(clean_env,monkeypatch):
    e=clean_env;await run(e)
    async def graph():yield e.driver
    monkeypatch.setattr('app.db.neo4j.get_neo4j',graph)
    async with e.r.sessions() as db:
        pack=ContextPack();await ContextPackBuilder(db=db)._build_facts(pack,str(e.r.project),2,2)
        card=next(c for c in pack.character_cards if c.name=='江枫')
        assert card.location=='酒馆' and card.chapter_state=={'状态':'酒馆'}
        assert card.mental_state==''
    await change(e,status='draft')
    async with e.r.sessions() as db:
        pack=ContextPack();await ContextPackBuilder(db=db)._build_facts(pack,str(e.r.project),2,2)
        card=next(c for c in pack.character_cards if c.name=='江枫')
        assert card.location=='' and card.mental_state=='' and card.chapter_state=={}


@pytest.mark.asyncio
async def test_pg_writer_location_ignores_a_derived_end_without_a_valid_later_source(clean_env):
    from app.services.character_locations import verified_character_locations
    e = clean_env
    await run(e)
    async with e.r.sessions() as db:
        await db.execute(update(CharacterLocation).values(chapter_end=1))
        await db.commit()
        rows = await verified_character_locations(db, e.r.project, 3)
        assert len(rows) == 1 and rows[0]['location'] == '酒馆'
        assert rows[0]['source_chapter_id'] == str(e.r.chapter)

@pytest.mark.asyncio
async def test_memory_snippet_header_does_not_claim_stale_roster_as_fact(clean_env):
    e=clean_env;await run(e)
    async with e.r.sessions() as db:
        db.add(CharacterAppearance(project_id=e.r.project,character_name='江枫',last_seen_chapter=5));await db.commit()
        pack=ContextPack();pack.character_cards=[CharacterCard(name='江枫')]
        await ContextPackBuilder(db=db)._build_returning_drilldown(pack,str(e.r.project),30)
        assert '最近核实片段 [CH-1]' in pack.returning_character_drilldown
        assert '[CH-5]' not in pack.returning_character_drilldown

@pytest.mark.parametrize('status,expected', [({'alive':False},True),({'status':'dead'},True),
    ({'状态':'死亡'},True),({'状态':'濒临死亡'},False),({'状态':'避免死亡'},False),({'alive':'false'},False)])
def test_death_warning_does_not_treat_mentions_as_actual_death(status,expected):
    from app.services.entity_context import explicitly_dead
    assert explicitly_dead(status) is expected

@pytest.mark.asyncio
async def test_generation_hook_does_not_warn_from_deleted_death_fact(clean_env,monkeypatch):
    from app.services.hook_manager import HookManager
    e=clean_env;data=payload();data['new_characters'][0]['state']={'alive':False}
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=data));await run(e)
    async def graph():yield e.driver
    monkeypatch.setattr('app.db.neo4j.get_neo4j',graph)
    async with e.r.sessions() as db:
        mgr=HookManager(db=db);monkeypatch.setattr(mgr,'_extract_character_names',lambda _:['江枫'])
        assert await mgr._check_character_consistency(e.r.project,2,{})
    await change(e,status='draft')
    async with e.r.sessions() as db:
        mgr=HookManager(db=db);monkeypatch.setattr(mgr,'_extract_character_names',lambda _:['江枫'])
        assert await mgr._check_character_consistency(e.r.project,2,{})==[]

@pytest.mark.asyncio
async def test_read_source_migration_roundtrip_preserves_author_data(clean_env):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    e=clean_env
    spec=importlib.util.spec_from_file_location('read_migration',
        '/root/ai-write/backend/alembic/versions/a1001927_entity_read_sources.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    async with e.r.engine.begin() as conn:
        def cycle(sync):
            module.op=Operations(MigrationContext.configure(sync));module.downgrade();module.upgrade()
        await conn.run_sync(cycle)
    await run(e)
    async with e.r.sessions() as db:
        assert (await db.get(Chapter,e.r.chapter)).content_text=='江枫留在酒馆。'
        assert (await db.scalar(select(CharacterState))).source_hash
        assert (await verified_world_snapshot(db,e.driver,e.r.project,2)).characters
