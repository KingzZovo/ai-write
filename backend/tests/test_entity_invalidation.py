"""Real PG/Neo4j deletion, cleanup recovery and stale-cleanup fencing."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, delete, update, text
from app.models import Base
from app.models.project import Chapter, Volume, Project, CharacterState, CharacterLocation, CharacterMemoryCard
from app.models.entity_extraction import EntityInvalidation as Pending
from app.services import entity_invalidation as inv, entity_projection as ep
from app.services.entity_graph_projection import project_entity_payload, ProjectionSuperseded
from app.services.entity_sources import digest
from app.services.entity_timeline import EntityTimelineService
from tests.test_entity_projection import env, source, payload, run
from tests.test_pipeline_control import runtime_db

@pytest_asyncio.fixture
async def clean_env(env, monkeypatch):
    e = env
    spec = importlib.util.spec_from_file_location('invalidations_migration',
        Path(__file__).parents[1] / 'alembic/versions/a1001926_entity_invalidations.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    async with e.r.engine.begin() as conn:
        def install(sync):
            Base.metadata.create_all(sync, tables=[t for t in Base.metadata.sorted_tables if t.name != Pending.__tablename__])
            sync.execute(text('DROP INDEX ix_character_memory_cards_source'))
            sync.execute(text('ALTER TABLE character_memory_cards DROP COLUMN source_hash, DROP COLUMN source_chapter_id'))
            migration.op = Operations(MigrationContext.configure(sync)); migration.upgrade()
        await conn.run_sync(install)
    monkeypatch.setattr('app.db.session.async_session_factory', e.r.sessions)
    from app.tasks.entity_tasks import _materialize_entities_to_postgres, _upsert_memory_cards
    e.materialize, e.cards, e.migration = _materialize_entities_to_postgres, _upsert_memory_cards, migration
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=payload()))
    return e

async def replay(e, materialize=None):
    return await inv.replay_entity_invalidations(sessions=e.r.sessions, engine=e.r.engine,
        driver=e.driver, materialize=materialize or e.materialize)

async def pending(e):
    async with e.r.sessions() as db:
        return (await db.execute(select(Pending.project_id, Pending.chapter_id, Pending.revision))).all()

async def change(e, **values):
    async with e.r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == e.r.chapter).values(**values)); await db.commit()

async def states(e):
    async with e.driver.session() as session:
        return await (await session.run('MATCH (:Character {project_id:$pid})-[:HAS_STATE]->(s) RETURN s.chapter_start AS start,s.chapter_end AS end,s.status_json AS state',pid=str(e.r.project))).data()

@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['chapter','volume','project'])
async def test_tombstones_survive_cascade_and_author_rollback(clean_env, target):
    e = clean_env
    async with e.r.sessions() as db:
        chapter = await db.get(Chapter,e.r.chapter)
        stmt = {'chapter':delete(Chapter).where(Chapter.id==e.r.chapter),
                'volume':delete(Volume).where(Volume.id==chapter.volume_id),
                'project':delete(Project).where(Project.id==e.r.project)}[target]
        await db.execute(stmt)
        assert await db.scalar(select(Pending)) is not None
        assert await pending(e) == []
        await db.rollback()
        assert await db.get(Chapter,e.r.chapter) is not None
        await db.execute(stmt); await db.commit()
    assert len(await pending(e)) == 1
    assert (await replay(e))['completed'] == 1
    assert await pending(e) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['delete','draft','empty'])
async def test_invalid_source_removes_owned_graph_read_models_and_cards(clean_env, mode):
    e = clean_env
    await run(e)
    async with e.r.sessions() as db:
        cards = (await db.scalars(select(CharacterMemoryCard))).all()
        assert cards and all(str(c.source_chapter_id)==str(e.r.chapter) and c.source_hash for c in cards)
        if mode=='delete': await db.execute(delete(Chapter).where(Chapter.id==e.r.chapter))
        elif mode=='draft': await db.execute(update(Chapter).where(Chapter.id==e.r.chapter).values(status='draft'))
        else: await db.execute(update(Chapter).where(Chapter.id==e.r.chapter).values(content_text=''))
        await db.commit()
    assert (await replay(e))['failed'] == 0
    assert await states(e) == []
    async with e.r.sessions() as db:
        assert await db.scalar(select(CharacterState)) is None
        assert await db.scalar(select(CharacterLocation)) is None
        assert await db.scalar(select(CharacterMemoryCard)) is None
    assert await pending(e) == []

@pytest.mark.asyncio
async def test_cleanup_after_new_projection_keeps_new_facts_and_cards(clean_env, monkeypatch):
    e=clean_env
    await run(e)
    await change(e,content_text='江枫抵达京城。',global_idx=2)
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=payload('江枫抵达京城。','京城')))
    await run(e)
    assert await pending(e)
    assert (await replay(e))['completed']==1
    assert len(await states(e))==1 and (await states(e))[0]['start']==2
    async with e.r.sessions() as db:
        cards=(await db.scalars(select(CharacterMemoryCard))).all()
        assert cards and all(c.global_idx==2 and '京城' in c.excerpt for c in cards)

@pytest.mark.asyncio
async def test_cleanup_failure_after_graph_commit_retries_pg_without_model(clean_env):
    e=clean_env
    await run(e); await change(e,status='draft')
    assert (await replay(e,AsyncMock(side_effect=RuntimeError('projection unavailable'))))['failed']==1
    assert await states(e)==[] and await pending(e)
    async with e.r.sessions() as db:
        assert (await db.scalar(select(Pending))).last_error=='RuntimeError'
        assert await db.scalar(select(CharacterState)) is not None
    assert (await replay(e))['completed']==1
    async with e.r.sessions() as db:
        assert await db.scalar(select(CharacterState)) is None
    ep.extract_entity_changes.assert_awaited_once()

@pytest.mark.asyncio
async def test_old_notification_cannot_acknowledge_later_author_change(clean_env):
    e=clean_env
    await run(e); await change(e,status='draft'); old=(await pending(e))[0]
    await change(e,title='作者再次编辑')
    await inv.invalidate_one(row=old,sessions=e.r.sessions,engine=e.r.engine,driver=e.driver,materialize=e.materialize)
    assert (await pending(e))[0].revision>old.revision
    await replay(e); assert await pending(e)==[]

@pytest.mark.asyncio
async def test_invalidation_waits_for_project_worker_and_remains_replayable(clean_env):
    e=clean_env
    await run(e); await change(e,status='draft')
    async with ep.entity_project_lock(e.r.engine,e.r.project):
        assert (await replay(e))['failed']==1
    assert await states(e) and await pending(e)
    assert (await replay(e))['completed']==1

@pytest.mark.asyncio
async def test_deleted_chapter_marker_fences_late_old_writer(clean_env):
    e=clean_env; src=await source(e.r)
    await run(e); await change(e,status='draft'); await replay(e)
    data=payload()
    with pytest.raises(ProjectionSuperseded):
        await project_entity_payload(e.driver,src,data,digest([src.source_hash,data]),1)
    assert await states(e)==[]

@pytest.mark.asyncio
async def test_cross_project_move_clears_only_old_project(clean_env):
    e=clean_env
    await run(e)
    async with e.r.sessions() as db:
        p=Project(title='新作品');db.add(p);await db.flush()
        v=Volume(project_id=p.id,title='新卷',volume_idx=1);db.add(v);await db.flush()
        await db.execute(update(Chapter).where(Chapter.id==e.r.chapter).values(volume_id=v.id));await db.commit()
    assert str((await pending(e))[0].project_id)==str(e.r.project)
    assert (await replay(e))['completed']==1 and await states(e)==[]
    async with e.r.sessions() as db:
        assert (await db.get(Chapter,e.r.chapter)).content_text=='江枫留在酒馆。'
        assert await db.scalar(select(CharacterMemoryCard)) is None

@pytest.mark.asyncio
async def test_removing_middle_state_reopens_earlier_and_preserves_later(clean_env):
    e=clean_env; first=await source(e.r)
    data=payload();await project_entity_payload(e.driver,first,data,digest([first.source_hash,data]),1)
    async with e.r.sessions() as db:
        ch=await db.get(Chapter,e.r.chapter)
        later=Chapter(volume_id=ch.volume_id,chapter_idx=2,global_idx=3,title='后章',content_text='江枫回家。',status='completed')
        db.add(later);await db.commit();later_id=later.id
    later_source=await source(e.r,later_id);data=payload('江枫回家。','家')
    await project_entity_payload(e.driver,later_source,data,digest([later_source.source_hash,data]),2)
    await EntityTimelineService(e.driver,strict_writes=True).update_character_state(str(e.r.project),'江枫',0,{'状态':'旧居'})
    await change(e,status='draft')
    assert (await replay(e))['completed']==1
    got=sorted(await states(e),key=lambda s:s['start'])
    assert [(s['start'],s['end']) for s in got]==[(0,2),(3,None)]

@pytest.mark.asyncio
async def test_migration_roundtrip_preserves_author_body(clean_env):
    e=clean_env
    async with e.r.engine.begin() as conn:
        def cycle(sync):
            e.migration.op=Operations(MigrationContext.configure(sync))
            e.migration.downgrade();e.migration.upgrade()
        await conn.run_sync(cycle)
    await change(e,status='draft'); assert await pending(e)
    async with e.r.sessions() as db:
        assert (await db.get(Chapter,e.r.chapter)).content_text=='江枫留在酒馆。'

@pytest.mark.asyncio
async def test_cancellation_keeps_cleanup_and_releases_project_lease(clean_env):
    import asyncio
    e=clean_env
    await run(e); await change(e,status='draft')
    with pytest.raises(asyncio.CancelledError):
        await replay(e,AsyncMock(side_effect=asyncio.CancelledError()))
    assert await pending(e)
    async with ep.entity_project_lock(e.r.engine,e.r.project) as lease:
        assert lease
    assert (await replay(e))['completed']==1

@pytest.mark.asyncio
async def test_stale_cleanup_token_cannot_erase_newer_graph(clean_env):
    from app.services.entity_graph_projection import invalidate_entity_projection
    e=clean_env;src=await source(e.r);data=payload()
    await project_entity_payload(e.driver,src,data,digest([src.source_hash,data]),100)
    with pytest.raises(ProjectionSuperseded):
        await invalidate_entity_projection(e.driver,project_id=str(e.r.project),chapter_id=str(e.r.chapter),current_hash=None,token=99)
    assert await states(e)

@pytest.mark.asyncio
async def test_volume_move_captures_old_project_before_membership_changes(clean_env):
    e=clean_env
    await run(e)
    async with e.r.sessions() as db:
        ch=await db.get(Chapter,e.r.chapter)
        p=Project(title='移入作品');db.add(p);await db.flush()
        await db.execute(update(Volume).where(Volume.id==ch.volume_id).values(project_id=p.id))
        await db.commit()
    assert str((await pending(e))[0].project_id)==str(e.r.project)
    assert (await replay(e))['completed']==1 and await states(e)==[]

@pytest.mark.asyncio
async def test_graph_cleanup_failure_is_atomic_and_retained(clean_env,monkeypatch):
    from app.services import entity_graph_projection as graph
    e=clean_env
    await run(e); await change(e,status='draft')
    repair=graph._repair_intervals
    monkeypatch.setattr(graph,'_repair_intervals',AsyncMock(side_effect=RuntimeError('late graph failure')))
    assert (await replay(e))['failed']==1
    assert await states(e) and await pending(e)
    monkeypatch.setattr(graph,'_repair_intervals',repair)
    assert (await replay(e))['completed']==1 and await states(e)==[]

@pytest.mark.asyncio
async def test_cleanup_preserves_unowned_legacy_cards(clean_env):
    e=clean_env
    await run(e)
    async with e.r.sessions() as db:
        db.add(CharacterMemoryCard(project_id=e.r.project,character_name='旧人物',global_idx=9,
                                   excerpt='未知来源旧卡',card_type='key_moment'))
        await db.commit()
    await change(e,status='draft'); await replay(e)
    async with e.r.sessions() as db:
        cards=(await db.scalars(select(CharacterMemoryCard))).all()
        assert len(cards)==1 and cards[0].source_chapter_id is None

@pytest.mark.asyncio
async def test_relationship_reconciliation_error_is_retained_for_retry(clean_env,monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.project import Relationship
    e=clean_env
    body='江枫与店主结交。';await change(e,content_text=body)
    data=payload(body);data['new_relationships']=[{'char1':'江枫','char2':'店主','type':'朋友','evidence':body}]
    monkeypatch.setattr(ep,'extract_entity_changes',AsyncMock(return_value=data))
    await run(e);await change(e,status='draft')
    execute=AsyncSession.execute
    async def fail_relationship_read(self, statement, *args, **kwargs):
        if 'FROM relationships' in str(statement) and 'relationships.source_id' in str(statement):
            raise RuntimeError('read model reconciliation failure')
        return await execute(self,statement,*args,**kwargs)
    monkeypatch.setattr(AsyncSession,'execute',fail_relationship_read)
    assert (await replay(e))['failed']==1 and await pending(e)
    monkeypatch.setattr(AsyncSession,'execute',execute)
    assert (await replay(e))['completed']==1
    async with e.r.sessions() as db:
        assert await db.scalar(select(Relationship)) is None
