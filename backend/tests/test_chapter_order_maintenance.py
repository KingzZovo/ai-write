"""Actual SQLite structural writes and HTTP failure paths; no live services."""
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.orm import Session

from app.api import chapters as chapter_api, volumes as volume_api
from app.db.session import get_db
from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.arc_progress import ArcProgressWrite
from app.models.project import Chapter, ChapterEvaluation, ChapterVersion, FilterWord, Outline, Project, Volume
from app.services import chapter_order as service


@pytest.fixture
def book(monkeypatch):
    engine = create_engine('sqlite://')
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    for model in (Project, Volume, Chapter, ChapterVersion, ChapterEvaluation, Outline, FilterWord, PipelineRun, PipelineChapterStatus, ArcProgressWrite):
        model.__table__.create(engine)
    with Session(engine) as session:
        pid, foreign, v1, v2, other_volume = [uuid4() for _ in range(5)]
        session.execute(insert(Project), [dict(id=pid, title='顺序试书', target_word_count=9000), dict(id=foreign, title='另一部书')])
        session.execute(insert(Volume), [dict(id=v1, project_id=pid, volume_idx=2, title='返乡'),
            dict(id=v2, project_id=pid, volume_idx=5, title='离乡'),
            dict(id=other_volume, project_id=foreign, volume_idx=1, title='别书')])
        ids = [uuid4() for _ in range(5)]
        rows = [dict(id=cid, volume_id=v1 if i < 3 else v2, chapter_idx=[4,9,20,1,10][i],
            global_idx=i+1, title=f'章名{i}', content_text=f'第{i}段作者正文。', status='draft') for i,cid in enumerate(ids)]
        outsider = uuid4()
        rows.append(dict(id=outsider, volume_id=other_volume, chapter_idx=1, global_idx=777,
            title='别书章名', content_text='别书正文。', status='draft'))
        session.execute(insert(Chapter), rows)
        bid = uuid4()
        session.execute(insert(Outline).values(id=bid, project_id=pid, level='book', is_confirmed=1,
            content_json={'raw_text':'已确认书纲', 'volume_plan':[{'idx':1,'est_chapters':3},{'idx':2,'est_chapters':3},{'idx':3,'est_chapters':3}]}))
        session.commit()
        r = NS(engine=engine, session=session, pid=pid, foreign=foreign, volumes=[v1,v2], ids=ids,
            outsider=outsider, queries=[], fail_commit=False, lose_ack=False, book_id=bid)
        async def execute(query, *args, **kwargs):
            r.queries.append(query)
            return session.execute(query, *args, **kwargs)
        async def scalar(query, *args, **kwargs):
            return (await execute(query, *args, **kwargs)).scalar()
        async def scalars(query, *args, **kwargs):
            return (await execute(query, *args, **kwargs)).scalars()
        async def get(model, identity, **kwargs):
            row = session.get(model, UUID(str(identity)))
            if kwargs.get('populate_existing') and row is not None:
                # Real AsyncSession re-reads the row; without this the identity map
                # hands back the stale in-memory object (e.g. pre-reindex global_idx).
                session.refresh(row)
            return row
        async def flush(): session.flush()
        async def refresh(row): session.refresh(row)
        async def remove(row): session.delete(row)
        async def rollback(): session.rollback()
        async def commit():
            if r.fail_commit:
                r.fail_commit = False
                raise RuntimeError('private commit failure')
            session.commit()
            if r.lose_ack:
                r.lose_ack = False
                raise RuntimeError('private acknowledgement failure')
        r.db = NS(execute=execute, scalar=scalar, scalars=scalars, get=get, add=session.add,
            flush=flush, refresh=refresh, delete=remove, rollback=rollback, commit=commit)
        r.writer_lock = AsyncMock(return_value=True)
        monkeypatch.setattr(service, 'try_command_lock', r.writer_lock)
        monkeypatch.setattr(service, 'require_materialization_lock', AsyncMock())
        async def database():
            try:
                yield r.db
                await r.db.commit()
            except BaseException:
                await r.db.rollback()
                raise
        app = FastAPI()
        app.include_router(chapter_api.router); app.include_router(volume_api.router)
        from app.api import arc, arc_progress
        app.include_router(arc.router)
        app.include_router(arc_progress.router)
        app.dependency_overrides[get_db] = database
        r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        yield r
    engine.dispose()


def saved(r):
    return {str(row['id']): dict(row) for row in r.session.execute(select(*Chapter.__table__.columns)).mappings()}


def order(r):
    rows = r.session.execute(select(Chapter.id, Chapter.global_idx).join(Volume).where(Volume.project_id == r.pid)
        .order_by(Volume.volume_idx, Chapter.chapter_idx)).all()
    assert [row.global_idx for row in rows] == list(range(1, len(rows)+1))
    return [row.id for row in rows]


def move(r, left=None, right=None):
    left, right = left or r.ids[0], right or r.ids[1]
    rows = saved(r)
    return f'/api/projects/{r.pid}/chapters/{left}/move', dict(neighbor_id=str(right),
        expected_version=service.chapter_order_version(rows[str(left)]),
        expected_neighbor_version=service.chapter_order_version(rows[str(right)]))


@pytest.mark.asyncio
async def test_insert_and_delete_in_earlier_volume_update_later_positions_without_touching_prose(book):
    r = book; before = saved(r)
    async with r.client() as client:
        created = await client.post(f'/api/projects/{r.pid}/chapters', json={
            'volume_id':str(r.volumes[0]), 'title':'插章', 'chapter_idx':7})
        assert created.status_code == 201, created.text
        added = UUID(created.json()['id'])
        assert created.json()['global_idx'] == 2
        assert order(r) == [r.ids[0],added,*r.ids[1:]]
        assert (await client.delete(f'/api/projects/{r.pid}/chapters/{r.ids[0]}')).status_code == 204
        assert order(r) == [added,*r.ids[1:]]
        r.queries.clear()
        chapters = (await client.get(f'/api/projects/{r.pid}/chapters?lightweight=true')).json()
        assert [row['global_idx'] for row in chapters] == [1,2,3,4,5]
        assert all(len(row['order_version']) == 64 for row in chapters)
        assert all('content_text' not in str(query) for query in r.queries)
    after = saved(r)
    assert after[str(r.outsider)] == before[str(r.outsider)]
    for cid in r.ids[1:]:
        for key in ('id','volume_id','chapter_idx','title','content_text','outline_json','summary','status'):
            assert after[str(cid)][key] == before[str(cid)][key]


@pytest.mark.asyncio
async def test_volume_reorder_and_delete_rebuild_the_complete_project(book):
    r = book
    async with r.client() as client:
        moved = await client.put(f'/api/projects/{r.pid}/volumes/{r.volumes[0]}', json={'volume_idx':7})
        assert moved.status_code == 200, moved.text
        assert order(r) == [*r.ids[3:],*r.ids[:3]]
        assert (await client.delete(f'/api/projects/{r.pid}/volumes/{r.volumes[1]}')).status_code == 204
        assert order(r) == r.ids[:3]


@pytest.mark.asyncio
async def test_occupied_labels_and_invalid_indices_cannot_leave_duplicates(book):
    r = book; before = saved(r)
    async with r.client() as client:
        duplicate = await client.put(f'/api/projects/{r.pid}/chapters/{r.ids[0]}',
            json={'chapter_idx':9,'title':'不能提前改掉章名'})
        assert duplicate.status_code == 409
        duplicate_volume = await client.post(f'/api/projects/{r.pid}/volumes', json={'title':'重复','volume_idx':2})
        assert duplicate_volume.status_code == 409
        for idx in (0, -1, True, 1.5, '3', 2147483648):
            bad = await client.post(f'/api/projects/{r.pid}/chapters',
                json={'volume_id':str(r.volumes[0]),'title':'无效','chapter_idx':idx})
            assert bad.status_code == 422
    assert saved(r) == before


@pytest.mark.asyncio
async def test_single_request_swaps_both_sides_and_rejects_old_retry_even_after_reverse(book, monkeypatch):
    r = book; before = saved(r)
    monkeypatch.setattr(service, '_utcnow', lambda: datetime(2000,1,1,tzinfo=timezone.utc))
    url, payload = move(r)
    async with r.client() as client:
        result = await client.post(url, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['chapter_idx'] == 9 and result.json()['neighbor_chapter_idx'] == 4
        assert order(r) == [r.ids[1],r.ids[0],*r.ids[2:]]
        assert (await client.post(url, json=payload)).status_code == 409
        reverse_url, reverse_payload = move(r)
        assert (await client.post(reverse_url, json=reverse_payload)).status_code == 200
        assert order(r) == r.ids
        assert (await client.post(url, json=payload)).status_code == 409
    for cid in r.ids:
        row = saved(r)[str(cid)]
        assert row['content_text'] == before[str(cid)]['content_text']
        assert row['title'] == before[str(cid)]['title']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['fail_commit','lose_ack'])
async def test_unconfirmed_move_never_returns_success_or_blindly_replays(book, failure):
    r = book; before = saved(r)
    url, payload = move(r)
    setattr(r, failure, True)
    async with r.client() as client:
        response = await client.post(url, json=payload)
        assert response.status_code == 503 and 'private' not in response.text
        if failure == 'fail_commit':
            assert saved(r) == before
            assert (await client.post(url, json=payload)).status_code == 200
        else:
            assert order(r) == [r.ids[1],r.ids[0],*r.ids[2:]]
            assert (await client.post(url, json=payload)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['foreign','other_volume','nonadjacent','stale','busy','queued'])
async def test_move_rejects_wrong_scope_stale_versions_and_generation_overlap(book, kind):
    r = book; before = saved(r)
    neighbor = {'foreign':r.outsider,'other_volume':r.ids[3],'nonadjacent':r.ids[2]}.get(kind)
    url, payload = move(r, right=neighbor)
    if kind == 'stale': payload['expected_version'] = '0'*64
    if kind == 'busy': r.writer_lock.return_value = False
    if kind == 'queued':
        r.session.execute(insert(PipelineRun).values(project_id=r.pid, state='queued')); r.session.commit()
    async with r.client() as client:
        response = await client.post(url, json=payload)
        assert response.status_code == (404 if kind == 'foreign' else 409), response.text
    assert saved(r) == before


@pytest.mark.asyncio
async def test_free_local_renumber_is_atomic_and_not_count_plus_label(book):
    r = book
    async with r.client() as client:
        changed = await client.put(f'/api/projects/{r.pid}/chapters/{r.ids[0]}', json={'chapter_idx':99})
        assert changed.status_code == 200, changed.text
        assert changed.json()['chapter_idx'] == 99 and changed.json()['global_idx'] == 3
        assert order(r) == [r.ids[1],r.ids[2],r.ids[0],*r.ids[3:]]


@pytest.mark.asyncio
async def test_large_book_reindex_reads_only_metadata_and_updates_all_positions_in_bounded_batches(book):
    r = book
    # 2000 additional chapters × 2500 characters = five million saved characters.
    ids = [uuid4() for _ in range(2000)]
    r.session.execute(insert(Chapter), [dict(id=cid, volume_id=r.volumes[1], title='长书章节',
        chapter_idx=i+100, global_idx=None, content_text='字'*2500) for i,cid in enumerate(ids)])
    r.session.commit(); r.queries.clear()
    await service.lock_chapter_structure(r.db, r.pid)
    assert await service.reindex_chapter_structure(r.db, r.pid) == 2000
    await r.db.commit()
    assert order(r) == r.ids + ids
    assert all('content_text' not in str(query) for query in r.queries)
    r.queries.clear()
    assert await service.reindex_chapter_structure(r.db, r.pid) == 0
    assert not any(str(query).startswith('UPDATE') for query in r.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['create','replace'])
async def test_generated_volume_materialization_also_updates_later_chapters(book, mode):
    from app.services.volume_creation import snapshot_creation, materialize_volume
    from app.services.volume_regeneration import snapshot_volume, replace_volume
    from tests.support.volume_meta import valid_volume_meta
    r = book
    index = 1 if mode == 'create' else 2
    parsed = {**valid_volume_meta(2), 'volume_idx':index, 'chapter_summaries':[
        {'chapter_idx':i,'title':f'新章{i}','summary':'过河后交还信件。','key_events':['过河','交信']} for i in (1,2)]}
    if mode == 'create':
        snapshot = await snapshot_creation(r.db, r.pid, r.book_id, index, 3)
        await materialize_volume(r.db, snapshot, parsed)
    else:
        snapshot = await snapshot_volume(r.db, r.pid, r.volumes[0])
        await replace_volume(r.db, snapshot, parsed)
    await r.db.commit()
    ids = order(r)
    assert ids[-2:] == r.ids[-2:]
    assert len(ids) == (7 if mode == 'create' else 4)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_arc_volume_save_is_atomic_and_cannot_duplicate_existing_volume(book, failure):
    from app.api.arc import StartArcBody, _save_generated_arc
    from fastapi import HTTPException
    from tests.support.arc_outline import arc_outline
    r = book; r.fail_commit = failure
    before = saved(r)
    body = StartArcBody(idea='新弧', core_setup='返乡兑现约定', target_chapters=4)
    if failure:
        with pytest.raises(HTTPException) as exc:
            await _save_generated_arc(r.db,str(r.pid),body,3,arc_outline(4, title='新弧'),4)
        assert exc.value.status_code == 503
        assert r.session.execute(select(Volume.id).where(Volume.project_id==r.pid,Volume.volume_idx==3)).first() is None
        assert saved(r) == before
        assert order(r) == r.ids
    else:
        await _save_generated_arc(r.db,str(r.pid),body,3,arc_outline(4, title='新弧'),4)
        with pytest.raises(HTTPException) as exc:
            await _save_generated_arc(r.db,str(r.pid),body,3,arc_outline(4, title='重复弧'),4)
        assert exc.value.status_code == 409
        positions = order(r)
        assert positions[:3] == r.ids[:3] and positions[-2:] == r.ids[-2:]
        assert len(positions) == len(r.ids) + 4
        after = saved(r)
        assert all(after[cid]['content_text'] == row['content_text'] for cid, row in before.items())


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['add','delete','move','outside_volume'])
async def test_pipeline_resume_revalidates_membership_and_displays_current_labels(book, monkeypatch, change):
    from app.services import pipeline_service
    from app.services.production import control
    r = book
    monkeypatch.setattr(control,'try_command_lock',r.writer_lock)
    ids = r.ids[:3] if change=='outside_volume' else r.ids
    pipeline = PipelineRun(project_id=r.pid, volume_id=r.volumes[0] if change=='outside_volume' else None,
        state='paused', total_chapters=len(ids))
    r.session.add(pipeline); r.session.flush()
    r.session.execute(insert(PipelineChapterStatus), [dict(pipeline_id=pipeline.id, chapter_id=cid,
        chapter_idx=saved(r)[str(cid)]['chapter_idx'], state='pending') for cid in ids])
    await r.db.commit()
    async with r.client() as client:
        if change in ('add','outside_volume'):
            response = await client.post(f'/api/projects/{r.pid}/chapters', json={
                'volume_id':str(r.volumes[1]),'title':'新增章','chapter_idx':99})
        elif change == 'delete':
            response = await client.delete(f'/api/projects/{r.pid}/chapters/{r.ids[0]}')
        else:
            url,payload=move(r)
            response=await client.post(url,json=payload)
        assert response.status_code in (200,201,204), response.text
    if change in ('add','delete'):
        with pytest.raises(ValueError,match='章节清单已改变'):
            await control.request_start(r.db,pipeline.id)
        await r.db.rollback()
    else:
        _,queued = await control.request_start(r.db,pipeline.id)
        assert queued
        status = await pipeline_service.get_pipeline_status(r.db,pipeline.id)
        if change == 'move':
            assert [row['chapter_id'] for row in status['chapters']][:2] == [str(r.ids[1]),str(r.ids[0])]
            assert [row['chapter_idx'] for row in status['chapters']][:2] == [4,9]
