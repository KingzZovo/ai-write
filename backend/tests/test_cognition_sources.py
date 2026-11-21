"""Real PostgreSQL cognition source/version/chronology and recovery boundaries."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import select, update, delete
from app.models.project import Chapter, Volume, CharacterCognition
from app.models.cognition import ChapterCognition as Record, CognitionRequest, CognitionBackfill, CognitionRevision
from app.services.context_errors import RequiredContextUnavailable
from app.services.character_cognition import load_ledger, _parse_changes
from app.services.cognition_extraction import run_cognition_extraction, replay_pending_cognition
from app.services.entity_sources import resolve_entity_source, digest
from app.services.entity_projection import now
from tests.test_pipeline_control import runtime_db

@pytest_asyncio.fixture
async def cog_db(runtime_db):
    r=runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: CharacterCognition.__table__.create(c))
        await conn.run_sync(lambda c: CognitionRequest.__table__.create(c))
        await conn.run_sync(lambda c: CognitionBackfill.__table__.create(c))
        await conn.run_sync(lambda c: CognitionRevision.__table__.create(c))
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter);ch.status='completed';ch.global_idx=1
        ch.content_text='沈青不知道父亲已经收到信。'
        db.add(CharacterCognition(project_id=r.project,character_name='沈青',knows=['旧账本的未来秘密']))
        await db.commit()
    return r

async def source(r,cid=None):
    async with r.sessions() as db:
        return await resolve_entity_source(db,r.project,0,cid or r.chapter)

async def extract(r,monkeypatch,changes,cid=None,*,cycle_id=None):
    model=AsyncMock(return_value=SimpleNamespace(text=json.dumps(changes,ensure_ascii=False)))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    result=await run_cognition_extraction(await source(r,cid),sessions=r.sessions,engine=r.engine,cycle_id=cycle_id)
    return result,model

@pytest.mark.asyncio
async def test_reads_reconstruct_before_target_without_legacy_or_future(cog_db,monkeypatch):
    r=cog_db
    unknown={'character':'沈青','still_unknown':'父亲收到信','evidence':'沈青不知道父亲已经收到信。'}
    await extract(r,monkeypatch,[unknown])
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter)
        second=Chapter(volume_id=ch.volume_id,chapter_idx=2,global_idx=2,status='completed',
                       title='第二章',content_text='父亲告诉沈青：“信收到了。”')
        db.add(second);await db.commit();cid=second.id
    learned={'character':'沈青','learns':'父亲收到信','evidence':'父亲告诉沈青：“信收到了。”'}
    await extract(r,monkeypatch,[learned],cid)
    async with r.sessions() as db:
        assert await load_ledger(db,r.project)=={}
        assert await load_ledger(db,r.project,before_chapter_id=r.chapter)=={}
        earlier=await load_ledger(db,r.project,before_chapter_id=cid)
        assert [(e['chapter_idx'],e['kind'],e['fact']) for e in earlier['沈青']['history']]==[(1,'still_unknown','父亲收到信')]
        later=await load_ledger(db,r.project,before_global_idx=3)
        assert [(e['chapter_idx'],e['kind'],e['fact']) for e in later['沈青']['history']]==[(1,'still_unknown','父亲收到信'),(2,'learns','父亲收到信')]
        await db.execute(update(Chapter).where(Chapter.id==cid).values(content_text='父亲没有提信的事。'))
        await db.commit()
        with pytest.raises(RequiredContextUnavailable, match='not_ready'):
            await load_ledger(db,r.project,before_global_idx=3)
        await db.rollback()
        await extract(r,monkeypatch,[],cid)
        assert await load_ledger(db,r.project,before_global_idx=3)==earlier

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['body','title','draft','delete','duplicate','move','tamper'])
async def test_invalid_sources_never_restore_old_knowledge(cog_db,monkeypatch,mutation):
    r=cog_db
    await extract(r,monkeypatch,[{'character':'__reader__','learns':'父亲收到信','evidence':'父亲已经收到信'}])
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter)
        if mutation=='body':ch.content_text='沈青走了。'
        elif mutation=='title':ch.title='改过标题'
        elif mutation=='draft':ch.status='draft'
        elif mutation=='delete':await db.execute(delete(Chapter).where(Chapter.id==r.chapter))
        elif mutation=='duplicate':db.add(Chapter(volume_id=ch.volume_id,chapter_idx=2,global_idx=1,title='重复章号'))
        elif mutation=='move':
            v=Volume(project_id=r.project,volume_idx=2,title='新卷');db.add(v);await db.flush();ch.volume_id=v.id
        elif mutation=='tamper':await db.execute(update(Record).values(changes_json=[]))
        await db.commit()
        if mutation not in {'draft', 'delete'}:
            with pytest.raises(RequiredContextUnavailable):
                await load_ledger(db,r.project,before_global_idx=2)
        else:
            assert await load_ledger(db,r.project,before_global_idx=2)=={}

@pytest.mark.asyncio
async def test_full_input_registry_task_and_completed_reuse(cog_db,monkeypatch):
    r=cog_db;quote='沈青终于知道父亲收到了信。'
    async with r.sessions() as db:
        ch=await db.get(Chapter,r.chapter);ch.content_text='风'*4000+quote+'雨'*4000;await db.commit()
    _,model=await extract(r,monkeypatch,[{'character':'沈青','learns':'父亲收到信','evidence':quote}])
    call=model.await_args.kwargs
    assert call['task_type']=='chapter_cognition' and call['db'] is None
    assert ('风'*4000+quote+'雨'*4000) in call['user_content']
    assert 'max_tokens' not in call
    await run_cognition_extraction(await source(r),sessions=r.sessions,engine=r.engine)
    model.assert_awaited_once()

@pytest.mark.asyncio
async def test_author_edit_during_extraction_discards_late_payload(cog_db,monkeypatch):
    r=cog_db;old=await source(r)
    async def model(**kwargs):
        async with r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(content_text='作者的新版本。'));await db.commit()
        return SimpleNamespace(text=json.dumps([{'character':'__reader__','learns':'旧事实','evidence':old.content}]))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    result=await run_cognition_extraction(old,sessions=r.sessions,engine=r.engine)
    assert result['status']=='obsolete'
    async with r.sessions() as db:
        assert (await db.scalar(select(Record))).changes_json is None
        with pytest.raises(RequiredContextUnavailable, match='not_ready'):
            await load_ledger(db,r.project,before_global_idx=2)

@pytest.mark.asyncio
async def test_failure_is_durable_without_periodic_paid_retries(cog_db,monkeypatch):
    r=cog_db
    model=AsyncMock(return_value=SimpleNamespace(text='[{"character":'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    with pytest.raises(ValueError):await run_cognition_extraction(await source(r),sessions=r.sessions,engine=r.engine)
    assert model.await_count==2
    async with r.sessions() as db:
        row=await db.scalar(select(Record));assert row.status=='failed' and row.changes_json is None
        row.updated_at=now()-timedelta(minutes=6);await db.commit()
    assert (await replay_pending_cognition(r.sessions,dispatch=lambda *args: None))['dispatched']==0


@pytest.mark.asyncio
async def test_pending_broker_failure_remains_retryable(cog_db):
    from app.services.cognition_extraction import enqueue_cognition
    r=cog_db
    async with r.sessions() as db:
        await enqueue_cognition(db, await source(r));await db.commit()
    def broken(*args):raise RuntimeError('broker unavailable')
    assert (await replay_pending_cognition(r.sessions,dispatch=broken))['dispatched']==0
    async with r.sessions() as db:
        row=await db.scalar(select(Record));assert row.status=='queued';row.updated_at=now()-timedelta(minutes=6);await db.commit()
    sent=[]
    assert (await replay_pending_cognition(r.sessions,dispatch=lambda *args:sent.append(args)))['dispatched']==1
    assert sent[0][1]==str(r.chapter)
    assert (await replay_pending_cognition(r.sessions,dispatch=lambda *args:sent.append(args)))['dispatched']==0

@pytest.mark.parametrize('raw',['','{}','[{"character":"甲"}]','[{"character":"甲","learns":"消息","evidence":"杜撰"}]',
    '[{"character":"甲","character":"乙","learns":"消息","evidence":"正文"}]'])
def test_parser_rejects_failure_instead_of_no_changes(raw):
    with pytest.raises(ValueError):_parse_changes(raw,'正文')

def test_parser_keeps_all_changes_and_empty_is_explicit():
    changes=[{'character':'沈青','learns':f'消息{i}','evidence':'正文'} for i in range(15)]
    assert _parse_changes(json.dumps(changes),'正文')==changes
    assert _parse_changes('[]','正文')==[]

@pytest.mark.asyncio
async def test_cancellation_is_retryable_and_releases_project_lease(cog_db,monkeypatch):
    from uuid import uuid4
    from app.services.cognition_reader import read_chapter_cognition
    from app.services.cognition_requests import retry_cognition
    r=cog_db;started=asyncio.Event()
    async def model(**kwargs):
        started.set();await asyncio.Event().wait()
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    task=asyncio.create_task(run_cognition_extraction(await source(r),sessions=r.sessions,engine=r.engine))
    await asyncio.wait_for(started.wait(),5);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    async with r.sessions() as db:
        assert (await db.scalar(select(Record))).status=='failed'
        view = await read_chapter_cognition(db, r.project, r.chapter)
        accepted = await retry_cognition(db, r.project, r.chapter, uuid4(), view['source_hash'], view['version'])
        await db.commit()
    result,_=await extract(r,monkeypatch,[],cycle_id=accepted['cycle_id'])
    assert result['status']=='completed'

@pytest.mark.asyncio
async def test_obsolete_source_can_be_requeued_when_author_restores_it(cog_db):
    from app.services.cognition_extraction import enqueue_cognition
    r=cog_db;src=await source(r)
    async with r.sessions() as db:
        await enqueue_cognition(db,src);await db.commit()
        row=await db.scalar(select(Record));row.status='obsolete';await db.commit()
        await enqueue_cognition(db,src);await db.commit();await db.refresh(row)
        assert row.status=='pending'
    sent=[]
    assert (await replay_pending_cognition(r.sessions,dispatch=lambda *args:sent.append(args)))['dispatched']==1

@pytest.mark.asyncio
async def test_registry_uses_cognition_schema_and_configured_budget(cog_db,monkeypatch):
    from tests.test_prompt_cache_transactions import tables
    from app.models.project import LLMEndpoint
    from app.models.prompt import PromptAsset
    from app.services import prompt_registry, prompt_cache
    r=cog_db;await tables(r)
    async with r.sessions() as db:
        endpoint=LLMEndpoint(name='extract',provider_type='openai',default_model='configured')
        db.add(endpoint);await db.flush()
        db.add(PromptAsset(task_type='extraction',name='reference',endpoint_id=endpoint.id,
            model_name='configured',max_tokens=9000,system_prompt='参考实体格式'))
        await db.commit();await prompt_registry.PromptRegistry(db).seed_builtins();await db.commit()
        asset=await db.scalar(select(PromptAsset).where(PromptAsset.task_type=='chapter_cognition'))
        assert asset.max_tokens==9000 and asset.endpoint_id==endpoint.id
        assert asset.output_schema['items']['oneOf'][0]['required']==['character','learns','evidence']
    prompt_cache.reset_for_tests()
    monkeypatch.setattr('app.db.session.async_session_factory',r.sessions)
    async def generate(task,messages,**kwargs):
        assert task=='chapter_cognition' and kwargs['route'].max_tokens==9000
        assert '参考实体格式' not in messages[0]['content']
        return SimpleNamespace(text='[]',usage=SimpleNamespace(total_tokens=1))
    monkeypatch.setattr(prompt_registry,'get_model_router_async',AsyncMock(return_value=SimpleNamespace(generate_with_tier_fallback=generate)))
    try:
        result=await run_cognition_extraction(await source(r),sessions=r.sessions,engine=r.engine)
        assert result['changes']==0
    finally:prompt_cache.reset_for_tests()

@pytest.mark.asyncio
async def test_migration_roundtrip_preserves_author_and_legacy_rows(cog_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r=cog_db
    spec=importlib.util.spec_from_file_location('cognition_migration',Path(__file__).parents[1]/'alembic/versions/a1001929_chapter_cognition.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    async with r.engine.begin() as conn:
        def cycle(sync):
            migration.op=Operations(MigrationContext.configure(sync))
            migration.downgrade();migration.upgrade();migration.downgrade();migration.upgrade()
        await conn.run_sync(cycle)
    async with r.sessions() as db:
        assert (await db.get(Chapter,r.chapter)).content_text=='沈青不知道父亲已经收到信。'
        assert (await db.scalar(select(CharacterCognition))).knows==['旧账本的未来秘密']
