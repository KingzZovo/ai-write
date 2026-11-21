from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update

from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter
from app.services import chapter_summarizer as cs


@pytest.mark.asyncio
@pytest.mark.parametrize('owned', [False, True])
@pytest.mark.parametrize('change', ['content', 'summary', 'title', 'status', 'delete', 'aba'])
async def test_stale_summary_does_not_overwrite_author_or_reach_vectors(runtime_db, monkeypatch, change, owned):
    r = runtime_db
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    source_text = '旧正文，人物尚未过河。' * 40
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = source_text
        chapter.summary = '原摘要'
        await db.commit()
    async def generate(**kwargs):
        assert kwargs['content_text'] == source_text
        async with r.sessions() as author:
            if change == 'delete':
                # This isolated schema has pipeline FKs, so deletion itself is
                # exercised separately with cascading application cleanup.
                from app.models.pipeline import PipelineChapterStatus
                await author.execute(delete(PipelineChapterStatus).where(PipelineChapterStatus.chapter_id == r.chapter))
                await author.execute(delete(Chapter).where(Chapter.id == r.chapter))
            else:
                changes = {'content': {'content_text': '作者已修改。'},
                           'summary': {'summary': '作者亲写摘要'},
                           'title': {'title': '作者新标题'},
                           'status': {'status': 'needs_review'},
                           'aba': {'content_text': '中间稿'}}[change]
                await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(**changes))
            await author.commit()
            if change == 'aba':
                await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text=source_text))
                await author.commit()
        return '由旧稿生成的摘要'
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    vector = AsyncMock()
    chunks = AsyncMock()
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', vector)
    monkeypatch.setattr('app.services.chapter_chunker.upsert_chapter_chunks', chunks)
    async with r.sessions() as db:
        assert await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=None if owned else db, overwrite=True) == (False, '')
    vector.assert_not_awaited()
    chunks.assert_not_awaited()
    async with r.sessions() as db:
        latest = await db.get(Chapter, r.chapter)
        if change == 'delete':
            assert latest is None
        else:
            assert latest.summary == ('作者亲写摘要' if change == 'summary' else '原摘要')
            if change == 'content':
                assert latest.content_text == '作者已修改。'


@pytest.mark.asyncio
async def test_current_summary_commits_before_projection(runtime_db, monkeypatch):
    r = runtime_db
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '人物已渡河。' * 60
        chapter.summary = None
        await db.commit()
    monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value='人物已渡河。'))
    async def vector(**kwargs):
        async with r.sessions() as verifier:
            latest = await verifier.get(Chapter, r.chapter)
            assert latest.summary == kwargs['summary'] == '人物已渡河。'
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', vector)
    monkeypatch.setattr('app.services.chapter_chunker.upsert_chapter_chunks', AsyncMock())
    async with r.sessions() as db:
        assert await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=db) == (True, '人物已渡河。')
        assert (await db.get(Chapter, r.chapter)).summary == '人物已渡河。'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'empty', 'cancel'])
async def test_owned_summary_releases_reads_before_model_and_writes_before_vectors(runtime_db, monkeypatch, outcome):
    import asyncio
    from contextlib import asynccontextmanager
    r = runtime_db
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '人物已渡河。' * 60
        chapter.summary = None
        await db.commit()
    phases = []
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            phases.append(db)
            yield db
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    async def generate(**kwargs):
        assert kwargs['db'] is None
        assert len(phases) == 1
        assert not phases[0].in_transaction()
        if outcome == 'cancel':
            raise asyncio.CancelledError()
        return '人物已渡河。' if outcome == 'success' else ''
    async def vector(**kwargs):
        assert len(phases) == 2
        assert all(not db.in_transaction() for db in phases)
        async with r.sessions() as verifier:
            latest = await verifier.get(Chapter, r.chapter)
            assert latest.summary == kwargs['summary'] == '人物已渡河。'
    projection = AsyncMock(side_effect=vector)
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    monkeypatch.setattr(cs, 'upsert_chapter_summary_embedding', projection)
    monkeypatch.setattr('app.services.chapter_chunker.upsert_chapter_chunks', AsyncMock())
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await cs.summarize_and_save_chapter(chapter_id=r.chapter)
    else:
        assert await cs.summarize_and_save_chapter(chapter_id=r.chapter) == (
            (True, '人物已渡河。') if outcome == 'success' else (False, ''))
    assert all(not db.in_transaction() for db in phases)
    if outcome != 'success':
        assert len(phases) == 1
        projection.assert_not_awaited()
        async with r.sessions() as verifier:
            assert (await verifier.get(Chapter, r.chapter)).summary is None
