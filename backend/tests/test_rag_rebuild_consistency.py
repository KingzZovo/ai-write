from unittest.mock import AsyncMock
import pytest
from sqlalchemy import update

from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Volume
from app.services.summary_provenance import summary_source_hash
from app.services import rag_rebuild as rr, chapter_summarizer as cs


async def prepare(r, summary=None):
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = '正文情节。' * 60
        ch.summary = summary
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, summary) if summary else None
        await db.commit()


@pytest.fixture
def no_progress(monkeypatch):
    monkeypatch.setattr(rr, 'redis_set_progress', AsyncMock())


@pytest.mark.asyncio
async def test_rebuild_commits_summary_before_vector(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    await prepare(r)
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='人物已过河。'))
    async def project(**kw):
        async with r.sessions() as db:
            assert (await db.get(Chapter, r.chapter)).summary == kw['summary'] == '人物已过河。'
        return True
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    async with r.sessions() as db:
        result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db)
    assert result == {'done': 1, 'total': 1, 'failed': [], 'status': 'completed'}


@pytest.mark.asyncio
async def test_vector_failure_reports_partial_and_retry_reuses_committed_summary(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    await prepare(r)
    generate = AsyncMock(return_value='合格格式的摘要。')
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    project = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    for expected in ['partial', 'completed']:
        async with r.sessions() as db:
            result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db)
            assert result['status'] == expected
    generate.assert_awaited_once()
    assert project.await_count == 2


@pytest.mark.asyncio
async def test_failed_generation_is_not_replaced_with_prose_prefix(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    await prepare(r)
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value=''))
    project = AsyncMock(return_value=True)
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    async with r.sessions() as db:
        result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db)
    assert result['done'] == 0 and result['failed'] == [str(r.chapter)]
    project.assert_not_awaited()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_force_controls_regeneration_of_existing_summary(runtime_db, monkeypatch, no_progress, force):
    r = runtime_db
    await prepare(r, '作者摘要')
    generate = AsyncMock(return_value='新摘要')
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    project = AsyncMock(return_value=True)
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    async with r.sessions() as db:
        result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db, force=force)
    assert result['status'] == 'completed'
    assert generate.await_count == int(force)
    assert project.call_args.kwargs['summary'] == ('新摘要' if force else '作者摘要')


@pytest.mark.asyncio
async def test_rebuild_rejects_concurrent_author_edit(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    await prepare(r)
    async def generate(**kw):
        async with r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='作者新正文'))
            await db.commit()
        return '过期摘要'
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    project = AsyncMock(return_value=True)
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    async with r.sessions() as db:
        result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db, force=True)
    assert result['status'] == 'partial'
    project.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebuild_follows_volume_order_and_continues_after_failure(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    await prepare(r, '前卷摘要')
    async with r.sessions() as db:
        old = await db.get(Chapter, r.chapter)
        old.chapter_idx = 99
        volume = Volume(project_id=r.project, title='后卷', volume_idx=2)
        db.add(volume)
        await db.flush()
        db.add(Chapter(volume_id=volume.id, title='后章', chapter_idx=1,
                       content_text='后文' * 200, summary='后卷摘要',
                       summary_source_hash=summary_source_hash('后章', '后文' * 200, '后卷摘要')))
        await db.commit()
    project = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(rr, 'upsert_chapter_summary_embedding', project)
    async with r.sessions() as db:
        result = await rr.rebuild_rag_for_project_async(project_id=r.project, db=db)
    assert [c.kwargs['summary'] for c in project.call_args_list] == ['前卷摘要', '后卷摘要']
    assert result['done'] == 1 and result['total'] == 2
    assert result['failed'] == [str(r.chapter)]


@pytest.mark.asyncio
async def test_rebuild_does_not_discard_pending_caller_changes(runtime_db, monkeypatch, no_progress):
    r = runtime_db
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.title = '尚未提交的作者标题'
        with pytest.raises(ValueError, match='pending author changes'):
            await rr.rebuild_rag_for_project_async(project_id=r.project, db=db)
        assert chapter.title == '尚未提交的作者标题'
        assert chapter in db.dirty
