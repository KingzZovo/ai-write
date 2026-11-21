import asyncio
import json
from uuid import UUID
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import select, event
from sqlalchemy.orm import Session

from tests.test_pipeline_control import runtime_db
from tests.support.volume_meta import valid_volume_meta
from app.models.project import Project, Volume, Chapter, Outline, FilterWord
from app.services.volume_creation import snapshot_creation, materialize_volume


@pytest_asyncio.fixture
async def creation(runtime_db):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Outline.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: FilterWord.__table__.create(c))
    async with r.sessions() as db:
        project = Project(title='新书', target_word_count=5400)
        db.add(project); await db.flush()
        book = Outline(project_id=project.id, level='book', is_confirmed=1,
                       content_json={'raw_text':'已确认书纲', 'volume_plan':[{'idx':1,'est_chapters':3}]})
        db.add(book); await db.commit()
        r.pid, r.bid = project.id, book.id
    return r


def payload():
    return {**valid_volume_meta(3), 'chapter_summaries':[
        {'chapter_idx':i,'title':f'第{i}场','summary':'渡过河流后交还信件。','key_events':['渡河','交信']} for i in range(1,4)
    ]}


async def snapshot(r):
    async with r.sessions() as db:
        return await snapshot_creation(db, r.pid, r.bid, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_budget', [None, 200000, 6000])
async def test_materializes_once_with_budget_and_preserves_custom_target(creation, existing_budget):
    r = creation
    if existing_budget:
        async with r.sessions() as db:
            db.add(Volume(project_id=r.pid, volume_idx=1, title='规划空卷', target_word_count=existing_budget));await db.commit()
    source = await snapshot(r)
    async with r.sessions() as db:
        outline = await materialize_volume(db, source, payload());await db.commit()
        volume = await db.scalar(select(Volume).where(Volume.project_id==r.pid))
        chapters = (await db.scalars(select(Chapter).where(Chapter.volume_id==volume.id))).all()
        expected = 6000 if existing_budget==6000 else 5400
        assert volume.target_word_count == expected
        assert len(chapters)==3 and all(c.target_word_count==expected//3 for c in chapters)
        assert outline.content_json['volume_id']==str(volume.id)
    with pytest.raises(ValueError, match='已有章节'):
        await snapshot(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['project','book','volume','chapter'])
async def test_author_changes_during_generation_are_preserved(creation, kind):
    r=creation
    async with r.sessions() as db:
        v=Volume(project_id=r.pid,volume_idx=1,title='空卷',target_word_count=5400);db.add(v);await db.commit();vid=v.id
    source=await snapshot(r)
    async with r.sessions() as db:
        if kind=='project': (await db.get(Project,r.pid)).target_word_count=6000
        elif kind=='book': (await db.get(Outline,r.bid)).content_json={'raw_text':'作者改稿'}
        elif kind=='volume': (await db.get(Volume,vid)).title='作者卷名'
        else: db.add(Chapter(volume_id=vid,chapter_idx=1,title='作者章节',content_text='作者原文。'))
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(ValueError): await materialize_volume(db,source,payload())
        await db.rollback()
        assert not (await db.scalars(select(Outline).where(Outline.project_id==r.pid,Outline.level=='volume'))).all()
        if kind=='chapter': assert (await db.scalar(select(Chapter).where(Chapter.volume_id==vid))).content_text=='作者原文。'


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_all_materialized_rows(creation):
    r=creation;source=await snapshot(r)
    async with r.sessions() as db:
        await materialize_volume(db,source,payload())
        def fail(session): raise RuntimeError('simulated commit failure')
        event.listen(db.sync_session,'before_commit',fail)
        try:
            with pytest.raises(RuntimeError): await db.commit()
        finally:
            event.remove(db.sync_session,'before_commit',fail)
            await db.rollback()
    async with r.sessions() as db:
        assert not (await db.scalars(select(Volume).where(Volume.project_id==r.pid))).all()
        assert not (await db.scalars(select(Outline).where(Outline.project_id==r.pid,Outline.level=='volume'))).all()


@pytest.mark.asyncio
async def test_concurrent_first_generation_cannot_create_duplicates(creation):
    r=creation;source=await snapshot(r)
    async def save():
        async with r.sessions() as db:
            try:
                await materialize_volume(db,source,payload());await db.commit();return True
            except ValueError:
                await db.rollback();return False
    assert sorted(await asyncio.gather(save(),save()))==[False,True]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['success','bad_json','commit_failure','cancel_save'])
async def test_stream_endpoint_only_confirms_atomic_success(creation,monkeypatch,mode):
    from app.api import generate as api
    from app.db import session
    r=creation
    monkeypatch.setattr(session._state,'sessionmaker',r.sessions)
    monkeypatch.setattr(session._state,'engine',r.engine)
    monkeypatch.setattr('app.services.model_router.get_model_router_async',AsyncMock())
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt',AsyncMock(return_value=''))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context',AsyncMock(return_value=[]))
    async def generate(self,**kwargs):
        async def stream():yield '{bad' if mode=='bad_json' else json.dumps(payload())
        return stream()
    monkeypatch.setattr(api.OutlineGenerator,'generate_volume_outline',generate)
    def fail(session):
        if mode=='commit_failure': raise RuntimeError('simulated commit failure')
    entered_save = asyncio.Event()
    if mode == 'cancel_save':
        async def paused_save(db, source, parsed):
            result = await materialize_volume(db, source, parsed)
            entered_save.set()
            await asyncio.Event().wait()
            return result
        monkeypatch.setattr('app.services.volume_creation.materialize_volume', paused_save)
    event.listen(Session,'before_commit',fail)
    try:
        async with r.sessions() as db:
            response=await api.generate_outline(api.GenerateOutlineRequest(project_id=str(r.pid),level='volume',
                parent_outline_id=str(r.bid),volume_idx=1,planned_volume_count=1),db=db)
            async def consume():
                return ''.join([part async for part in response.body_iterator])
            if mode == 'cancel_save':
                consumer = asyncio.create_task(consume())
                await asyncio.wait_for(entered_save.wait(), timeout=5)
                consumer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await consumer
                events = ''
            else:
                events = await consume()
    finally: event.remove(Session,'before_commit',fail)
    if mode=='success': assert '"status": "saved"' in events and '"status": "completed"' in events
    else: assert '"status": "saved"' not in events and '"status": "completed"' not in events
    async with r.sessions() as db:
        volumes=(await db.scalars(select(Volume).where(Volume.project_id==r.pid))).all()
        assert len(volumes)==(1 if mode=='success' else 0)


@pytest.mark.asyncio
async def test_weighted_budgets_preserve_remainder_across_volumes(creation):
    r = creation
    async with r.sessions() as db:
        (await db.get(Project, r.pid)).target_word_count = 5401
        (await db.get(Outline, r.bid)).content_json = {
            'volume_plan': [{'idx': 1, 'est_chapters': 1}, {'idx': 2, 'est_chapters': 2}]}
        await db.commit()
    targets = []
    for idx in (1, 2):
        async with r.sessions() as db:
            source = await snapshot_creation(db, r.pid, r.bid, idx, 2)
            parsed = payload()
            parsed['volume_idx'] = idx
            outline = await materialize_volume(db, source, parsed)
            await db.commit()
            volume = await db.get(Volume, UUID(outline.content_json['volume_id']))
            chapters = (await db.scalars(select(Chapter).where(Chapter.volume_id == volume.id))).all()
            assert sum(c.target_word_count for c in chapters) == volume.target_word_count
            targets.append(volume.target_word_count)
    assert targets == [1800, 3601]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['wrong_project', 'unconfirmed', 'invalid_plan'])
async def test_invalid_parent_rejected_before_generation(creation, kind):
    r = creation
    async with r.sessions() as db:
        book = await db.get(Outline, r.bid)
        if kind == 'wrong_project':
            book.project_id = r.project
        elif kind == 'unconfirmed':
            book.is_confirmed = 0
        else:
            book.content_json = {'volume_plan': [{'idx': 2, 'est_chapters': 3}]}
        await db.commit()
    with pytest.raises(ValueError):
        await snapshot(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['wrong_index', 'missing_chapter', 'duplicate_chapter'])
async def test_invalid_generated_structure_never_materializes(creation, kind):
    r = creation
    source = await snapshot(r)
    parsed = payload()
    if kind == 'wrong_index':
        parsed['volume_idx'] = 2
    elif kind == 'missing_chapter':
        parsed['chapter_summaries'].pop()
    else:
        parsed['chapter_summaries'][1]['chapter_idx'] = 1
    async with r.sessions() as db:
        with pytest.raises(ValueError):
            await materialize_volume(db, source, parsed)
        await db.rollback()
        assert not (await db.scalars(select(Volume).where(Volume.project_id == r.pid))).all()
