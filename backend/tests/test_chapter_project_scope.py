"""Project paths must not read or mutate chapters belonging to another book."""
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, func

from tests.test_pipeline_control import runtime_db
from app.api import chapters
from app.db.session import get_db
from app.models.project import Project, Chapter


@pytest_asyncio.fixture
async def scoped_client(runtime_db, monkeypatch):
    r = runtime_db
    async with r.sessions() as db:
        other = Project(title='另一部隔离测试小说')
        db.add(other)
        await db.commit()
        chapter = await db.get(Chapter, r.chapter)
        volume_id = str(chapter.volume_id)
        foreign_id = str(other.id)
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_for_chapter', AsyncMock())
    async def database():
        async with r.sessions() as db:
            yield db
            await db.commit()
    app = FastAPI()
    app.include_router(chapters.router)
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client, foreign_id, volume_id


@pytest.mark.asyncio
@pytest.mark.parametrize('method,suffix,payload', [
    ('GET', '', None),
    ('PUT', '', {'title': '不能串书改标题', 'content_text': '不能串书改正文', 'expected_content_text': '原稿。'}),
    ('DELETE', '', None),
    ('POST', '/sync', {'old_text': '原稿。', 'new_text': '不能串书同步'}),
    ('POST', '/outline/expand', None),
])
async def test_foreign_chapter_rejected_before_mutation_or_model(runtime_db, scoped_client, monkeypatch, method, suffix, payload):
    client, foreign_id, _ = scoped_client
    expansion = AsyncMock()
    sync = AsyncMock()
    monkeypatch.setattr('app.services.chapter_outline_expander.expand_chapter_outline', expansion)
    monkeypatch.setattr('app.services.incremental_sync.IncrementalSyncService.process_edit', sync)
    response = await client.request(method, f'/api/projects/{foreign_id}/chapters/{runtime_db.chapter}{suffix}', json=payload)
    assert response.status_code == 404
    expansion.assert_not_awaited()
    sync.assert_not_awaited()
    async with runtime_db.sessions() as db:
        chapter = await db.get(Chapter, runtime_db.chapter)
        assert chapter.title == '第一章' and chapter.content_text == '原稿。'


@pytest.mark.asyncio
@pytest.mark.parametrize('lightweight', [False, True])
async def test_volume_filter_cannot_bypass_project_scope(runtime_db, scoped_client, lightweight):
    client, foreign_id, volume_id = scoped_client
    query = {'volume_id': volume_id, 'lightweight': str(lightweight).lower()}
    wrong = await client.get(f'/api/projects/{foreign_id}/chapters', params=query)
    assert wrong.status_code == 200 and wrong.json() == []
    right = await client.get(f'/api/projects/{runtime_db.project}/chapters', params=query)
    assert right.status_code == 200
    assert [row['id'] for row in right.json()] == [str(runtime_db.chapter)]


@pytest.mark.asyncio
async def test_create_rejects_foreign_volume_but_allows_own(runtime_db, scoped_client):
    client, foreign_id, volume_id = scoped_client
    payload = {'volume_id': volume_id, 'title': '第二章', 'chapter_idx': 2}
    wrong = await client.post(f'/api/projects/{foreign_id}/chapters', json=payload)
    assert wrong.status_code == 404
    async with runtime_db.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Chapter)) == 1
    right = await client.post(f'/api/projects/{runtime_db.project}/chapters', json=payload)
    assert right.status_code == 201
    assert right.json()['volume_id'] == volume_id


@pytest.mark.asyncio
async def test_own_chapter_reads_and_edits_still_work(runtime_db, scoped_client):
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    read = await client.get(path)
    assert read.status_code == 200 and read.json()['content_text'] == '原稿。'
    edited = await client.put(path, json={'title': '作者修订标题', 'content_text': '作者修订正文。', 'expected_content_text': read.json()['content_text']})
    assert edited.status_code == 200
    assert edited.json()['title'] == '作者修订标题'
    assert (await client.get(path)).json()['content_text'] == '作者修订正文。'
    absent = await client.get(f'/api/projects/{uuid4()}/chapters/{runtime_db.chapter}')
    assert absent.status_code == 404


@pytest.mark.asyncio
async def test_conditional_save_conflict_does_not_change_body_title_or_status(runtime_db, scoped_client):
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    first = await client.put(path, json={'content_text': '作者的新稿。', 'expected_content_text': '原稿。', 'status': 'needs_review'})
    assert first.status_code == 200
    stale = await client.put(path, json={'content_text': '迟到旧稿。', 'expected_content_text': '原稿。', 'title': '错误标题', 'status': 'completed', 'force': True})
    assert stale.status_code == 409
    current = (await client.get(path)).json()
    assert current['content_text'] == '作者的新稿。'
    assert current['title'] == '第一章' and current['status'] == 'needs_review'


@pytest.mark.asyncio
async def test_concurrent_conditional_saves_have_one_winner(runtime_db, scoped_client):
    import asyncio
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    responses = await asyncio.gather(*[
        client.put(path, json={'content_text': text, 'expected_content_text': '原稿。'})
        for text in ('第一份改稿。', '第二份改稿。')
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r.json()['content_text'] for r in responses if r.status_code == 200)
    assert (await client.get(path)).json()['content_text'] == winner


@pytest.mark.asyncio
async def test_outline_conditional_save_preserves_new_outline_and_body(runtime_db, scoped_client):
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    original = (await client.get(path)).json()
    baseline = original['outline_json'] or {}
    result = await client.put(path, json={'outline_json': {'summary': '新细纲'}, 'expected_outline_json': baseline})
    assert result.status_code == 200
    stale = await client.put(path, json={'outline_json': {'summary': '过期细纲'}, 'expected_outline_json': baseline,
        'content_text': '也不能覆盖正文', 'expected_content_text': original['content_text'] or '', 'title': '错误标题', 'force': True})
    assert stale.status_code == 409
    saved = (await client.get(path)).json()
    assert saved['outline_json'] == {'summary': '新细纲'}
    assert saved['content_text'] == original['content_text'] and saved['title'] == original['title']


@pytest.mark.asyncio
async def test_concurrent_outline_conditional_saves_have_one_winner(runtime_db, scoped_client):
    import asyncio
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    original = (await client.get(path)).json()['outline_json'] or {}
    responses = await asyncio.gather(*[
        client.put(path, json={'outline_json': {'summary': text}, 'expected_outline_json': original})
        for text in ('第一份细纲', '第二份细纲')
    ])
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()['outline_json'] for response in responses if response.status_code == 200)
    assert (await client.get(path)).json()['outline_json'] == winner


@pytest.mark.asyncio
async def test_author_text_revision_is_saved_and_used_without_old_structure(runtime_db, scoped_client):
    from app.services.context_pack import ContextPack
    client, _, _ = scoped_client
    path = f'/api/projects/{runtime_db.project}/chapters/{runtime_db.chapter}'
    original = {'summary': '母亲已经去世', 'key_events': ['办葬礼'], 'raw_text': '旧稿'}
    created = await client.put(path, json={'outline_json': original})
    assert created.status_code == 200
    current = created.json()['outline_json']
    revised = {**original, 'raw_text': '母亲还活着，林禾给她打电话。'}
    response = await client.put(path, json={'outline_json': revised, 'expected_outline_json': current})
    assert response.status_code == 200
    saved = (await client.get(path)).json()['outline_json']
    assert saved['summary'] == original['summary'] and saved['_author_text_revision']
    prompt = ContextPack()._render_chapter_outline_block(saved)
    assert revised['raw_text'] in prompt and original['summary'] not in prompt
