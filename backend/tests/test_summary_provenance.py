from unittest.mock import AsyncMock
from pathlib import Path
import importlib.util

import pytest
from sqlalchemy import update
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.project import Chapter
from app.services import chapter_summarizer as cs
from app.services.summary_provenance import summary_is_current, summary_source_hash
from tests.test_pipeline_control import runtime_db


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unchanged', 'content', 'title', 'summary', 'legacy'])
async def test_reuse_requires_source_and_output_match(runtime_db, monkeypatch, change):
    r = runtime_db
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '人物尚未过河。' * 60
        chapter.summary = '人物还在岸边。'
        chapter.summary_source_hash = summary_source_hash(chapter.title, chapter.content_text, chapter.summary)
        await db.commit()
    values = {
        'content': {'content_text': '人物已经过河。' * 60},
        'title': {'title': '新标题'},
        'summary': {'summary': '规划中的结局'},
        'legacy': {'summary_source_hash': None},
    }
    if change in values:
        async with r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(**values[change]))
            await db.commit()
    generate = AsyncMock(return_value='根据当前正文生成的新摘要。')
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    async with r.sessions() as db:
        written, summary = await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=db, project_vectors=False)
        assert written == (change != 'unchanged')
        assert generate.await_count == int(change != 'unchanged')
        assert summary == ('人物还在岸边。' if change == 'unchanged' else '根据当前正文生成的新摘要。')
    async with r.sessions() as db:
        assert summary_is_current(await db.get(Chapter, r.chapter))


@pytest.mark.asyncio
async def test_failed_regeneration_does_not_return_stale_summary(runtime_db, monkeypatch):
    async with runtime_db.sessions() as db:
        chapter = await db.get(Chapter, runtime_db.chapter)
        chapter.content_text = '新正文' * 100
        chapter.summary = '旧正文摘要'
        await db.commit()
        monkeypatch.setattr(cs, 'summarize_chapter_text', AsyncMock(return_value=''))
        assert await cs.summarize_and_save_chapter(chapter_id=chapter.id, db=db, project_vectors=False) == (False, '')
        assert chapter.summary == '旧正文摘要'
        assert not summary_is_current(chapter)


@pytest.mark.asyncio
async def test_concurrent_source_stamp_edit_is_not_overwritten(runtime_db, monkeypatch):
    r = runtime_db
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = '原正文' * 100
        await db.commit()
    async def generate(**kwargs):
        async with r.sessions() as author:
            await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(summary_source_hash='a' * 64))
            await author.commit()
        return '迟到的模型结果'
    monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
    async with r.sessions() as db:
        assert await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=db, project_vectors=False) == (False, '')


@pytest.mark.asyncio
async def test_migration_preserves_legacy_summary_without_fabricating_provenance(runtime_db):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/a1001922_summary_provenance.py'
    spec = importlib.util.spec_from_file_location('summary_provenance_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with runtime_db.engine.begin() as conn:
        def migrate(sync):
            module.op = Operations(MigrationContext.configure(sync))
            module.downgrade()
            module.upgrade()
            module.upgrade()
        await conn.run_sync(migrate)
    async with runtime_db.sessions() as db:
        ch = await db.get(Chapter, runtime_db.chapter)
        assert ch.summary == '原摘要'
        assert ch.summary_source_hash is None
        assert not summary_is_current(ch)


@pytest.mark.asyncio
async def test_cached_summary_is_not_reused_after_external_edit(runtime_db, monkeypatch):
    r = runtime_db
    async with r.sessions() as stale:
        ch = await stale.get(Chapter, r.chapter)
        ch.content_text = '原文' * 150
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        await stale.commit()
        async with r.sessions() as author:
            await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='作者新正文' * 100))
            await author.commit()
        generate = AsyncMock()
        monkeypatch.setattr(cs, 'summarize_chapter_text', generate)
        assert await cs.summarize_and_save_chapter(chapter_id=r.chapter, db=stale, project_vectors=False) == (False, '')
        generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_author_summary_is_not_committed_by_reuse_check(runtime_db, monkeypatch):
    async with runtime_db.sessions() as db:
        ch = await db.get(Chapter, runtime_db.chapter)
        ch.content_text = '正文' * 150
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        await db.commit()
        ch.summary = '尚未提交的作者摘要'
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        assert await cs.summarize_and_save_chapter(chapter_id=ch.id, db=db, project_vectors=False) == (False, '')
        assert ch in db.dirty
        async with runtime_db.sessions() as verifier:
            assert (await verifier.get(Chapter, ch.id)).summary == '原摘要'


@pytest.mark.asyncio
async def test_pipeline_rollback_restores_summary_provenance_with_source(runtime_db):
    from tests.test_pipeline_control import write_generated
    from app.services.production.control import request_rollback
    r = runtime_db
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        await db.commit()
    await write_generated(r)
    async with r.sessions() as db:
        assert not summary_is_current(await db.get(Chapter, r.chapter))
        await request_rollback(db, r.pipeline)
        await db.commit()
    async with r.sessions() as db:
        assert summary_is_current(await db.get(Chapter, r.chapter))
