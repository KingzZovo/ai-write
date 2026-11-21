"""Real PostgreSQL coverage for formerly independent chapter-memory writers."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models.project import Chapter, Volume
from app.services import chapter_summarizer as cs
from app.services.hook_manager import HookManager
from app.services.memory import HierarchicalMemory
from app.services.incremental_sync import IncrementalSyncService
from app.services.summary_provenance import summary_is_current
from tests.test_pipeline_control import runtime_db

SOURCE = '他拿着四百文钱，在岸边等信使。' * 350

async def prepare(r):
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = SOURCE
        ch.status = 'completed'
        ch.global_idx = 1
        await db.commit()
        return ch.volume_id

@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['hook', 'memory'])
@pytest.mark.parametrize('case', ['valid', 'wrong_project', 'old_source', 'racing_edit', 'failed'])
async def test_scoped_routes_commit_only_matching_source(runtime_db, monkeypatch, entry, case):
    r = runtime_db
    volume = await prepare(r)
    async def generate(**kwargs):
        assert kwargs['content_text'] == SOURCE
        if case == 'racing_edit':
            async with r.sessions() as author:
                await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='作者改稿' * 100))
                await author.commit()
        return '' if case == 'failed' else '他拿着四百文，在岸边等信使。'
    model = AsyncMock(side_effect=generate)
    monkeypatch.setattr(cs, 'summarize_chapter_text', model)
    projected = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', projected)
    monkeypatch.setattr('app.services.chapter_chunker.upsert_chapter_chunks', AsyncMock())
    async with r.sessions() as db:
        # Another volume has the same local chapter number.
        other = Volume(project_id=r.project, volume_idx=2, title='第二卷')
        db.add(other)
        await db.flush()
        sibling = Chapter(volume_id=other.id, chapter_idx=1, title='同号章', content_text=SOURCE, summary='不得改写')
        db.add(sibling)
        await db.commit()
        project = uuid4() if case == 'wrong_project' else r.project
        text = '过期派发正文' if case == 'old_source' else SOURCE
        if entry == 'hook':
            await HookManager(db=db)._generate_summary(project, volume, 1, text)
        else:
            await HierarchicalMemory(db=db, qdrant_client=object()).generate_chapter_summary(
                text, 1, str(project), str(volume))
        assert sibling.summary == '不得改写'
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert summary_is_current(ch) == (case == 'valid')
        assert projected.await_count == int(case == 'valid')
        if case == 'valid':
            assert projected.call_args.kwargs['chapter_id'] == r.chapter
            assert projected.call_args.kwargs['source_hash'] == ch.summary_source_hash
        else:
            assert ch.summary == '原摘要'
    assert model.await_count == int(case not in ('wrong_project', 'old_source'))

@pytest.mark.asyncio
async def test_text_only_memory_uses_complete_registry_input_without_fallback(monkeypatch):
    from app.services import prompt_registry
    generate = AsyncMock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', generate)
    result = await HierarchicalMemory(db=object()).generate_chapter_summary(SOURCE, 1)
    assert result == ''
    assert SOURCE in generate.call_args.kwargs['user_content']

@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'unsaved', 'dirty', 'failed', 'vector_failed', 'small_edit'])
async def test_incremental_summary_is_generated_once_and_projects_committed_output(runtime_db, monkeypatch, case):
    r = runtime_db
    await prepare(r)
    model = AsyncMock(return_value='' if case == 'failed' else '已提交的唯一摘要')
    monkeypatch.setattr(cs, 'summarize_chapter_text', model)
    async def project(**kwargs):
        async with r.sessions() as reader:
            saved = await reader.get(Chapter, r.chapter)
            assert summary_is_current(saved)
            assert saved.summary == kwargs['summary']
            assert saved.summary_source_hash == kwargs['source_hash']
        return case != 'vector_failed'
    vector = AsyncMock(side_effect=project)
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', vector)
    async with r.sessions() as db:
        svc = IncrementalSyncService(db)
        svc._update_entities = AsyncMock(return_value=['diagnostic'])
        svc._check_foreshadow_impact = AsyncMock(return_value=[])
        if case == 'dirty':
            ch = await db.get(Chapter, r.chapter)
            ch.title = '未提交作者标题'
        old = SOURCE.replace('四', '五', 1) if case == 'small_edit' else '旧正文'
        result = await svc.process_edit(str(r.chapter), old, '未保存正文' if case == 'unsaved' else SOURCE)
        assert result['synced'] is False  # Entity extraction does not persist graph changes.
        if case in ('unsaved', 'dirty'):
            assert result['reason'] == 'source_mismatch'
            model.assert_not_awaited()
        elif case == 'failed':
            assert result['reason'] == 'summary_failed_or_stale'
        else:
            assert result['reason'] == 'partial_sync'
            assert result['details']['summary_updated']
            assert not result['details']['entities_updated']
            assert result['details']['qdrant_updated'] == (case != 'vector_failed')
        assert vector.await_count == int(case in ('valid', 'vector_failed', 'small_edit'))
        assert model.await_count <= 1

@pytest.mark.asyncio
async def test_outline_cascade_preserves_actual_summary(runtime_db):
    from app.models.project import Outline
    from app.api.outlines import update_outline, OutlineUpdate
    from app.services.summary_provenance import summary_source_hash

    r = runtime_db
    await prepare(r)
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: Outline.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.summary = '信使尚未到岸。'
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        outline = Outline(project_id=r.project, level='volume', content_json={})
        db.add(outline)
        await db.commit()
        await update_outline(str(r.project), str(outline.id), OutlineUpdate(content_json={
            'volume_idx': 1, 'chapter_summaries': [{'summary': '信使交信后离开。'}],
        }), db)
        await db.commit()
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert ch.outline_json.get('summary') != '信使交信后离开。'  # authored prose blocks automatic planning replacement
        assert ch.summary == '信使尚未到岸。'
        assert summary_is_current(ch)

@pytest.mark.asyncio
async def test_sync_does_not_project_after_intervening_edit(runtime_db, monkeypatch):
    r = runtime_db
    await prepare(r)
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='原派发正文的摘要'))
    projected = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', projected)
    async def edit(*args):
        async with r.sessions() as author:
            await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='另一份改稿' * 100))
            await author.commit()
        return []
    async with r.sessions() as db:
        svc = IncrementalSyncService(db)
        svc._update_entities = edit
        svc._check_foreshadow_impact = AsyncMock(return_value=[])
        result = await svc.process_edit(str(r.chapter), '旧正文', SOURCE)
        assert not result['details']['qdrant_updated']
    projected.assert_not_awaited()

@pytest.mark.asyncio
async def test_sync_endpoint_rejects_foreign_project(runtime_db, monkeypatch):
    from fastapi import HTTPException
    from app.api.chapters import sync_chapter_edit, ChapterSyncRequest
    called = AsyncMock()
    monkeypatch.setattr(IncrementalSyncService, 'process_edit', called)
    async with runtime_db.sessions() as db:
        with pytest.raises(HTTPException) as exc:
            await sync_chapter_edit(str(uuid4()), str(runtime_db.chapter),
                                    ChapterSyncRequest(old_text='旧', new_text='新'), db)
        assert exc.value.status_code == 404
    called.assert_not_awaited()

@pytest.mark.asyncio
async def test_scoped_source_moved_before_dispatch_is_not_reloaded(runtime_db, monkeypatch):
    r = runtime_db
    volume = await prepare(r)
    async with r.sessions() as db:
        other = Volume(project_id=r.project, volume_idx=2, title='新位置')
        db.add(other)
        await db.commit()
        destination = other.id
    original = cs.summarize_and_save_chapter
    async def moved(**kwargs):
        async with r.sessions() as author:
            await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(volume_id=destination))
            await author.commit()
        return await original(**kwargs)
    monkeypatch.setattr(cs, 'summarize_and_save_chapter', moved)
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='旧位置派发的摘要'))
    vector = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', vector)
    async with r.sessions() as db:
        assert await cs.summarize_scoped_chapter(db=db, project_id=r.project,
            volume_id=volume, chapter_idx=1, chapter_text=SOURCE) == (False, '')
    vector.assert_not_awaited()
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert ch.volume_id == destination
        assert ch.summary == '原摘要'
