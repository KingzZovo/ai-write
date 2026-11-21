from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from tests.test_context_cross_volume import seed
from app.models.project import Chapter
from app.services.summary_provenance import summary_source_hash, current_summary_sql
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.evaluation_neighbors import load_evaluation_neighbors
from app.services.memory import HierarchicalMemory


@pytest.mark.asyncio
@pytest.mark.parametrize('title,body,summary', [
    ('雨夜', '雨停了。', '人物过桥。'),
    ('引号"与\\', '中文😀\n第二段\t尾', '摘要\n保留。'),
    ('12:数字', '3:标题与正文', '分帧不能混淆'),
])
async def test_sql_fingerprint_matches_python_without_returning_full_prose(runtime_db, title, body, summary):
    async with runtime_db.sessions() as db:
        chapter = await db.get(Chapter, runtime_db.chapter)
        chapter.title, chapter.content_text, chapter.summary = title, body, summary
        chapter.summary_source_hash = summary_source_hash(title, body, summary)
        await db.commit()
        query = select(current_summary_sql()).where(Chapter.id == chapter.id)
        assert await db.scalar(query) == summary
        chapter.content_text += '改稿'
        await db.commit()
        assert await db.scalar(query) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'summary', 'legacy'])
async def test_stale_summary_cannot_enter_proximity_or_evaluation(runtime_db, change):
    r = runtime_db
    _, v2, _, _ = await seed(r)
    async with r.sessions() as db:
        previous = await db.scalar(select(Chapter).where(Chapter.chapter_idx == 100))
        if change == 'body':
            previous.content_text = '作者新正文：人物没有过桥。'
        elif change == 'summary':
            previous.summary = '大纲规划冒充事实'
        else:
            previous.summary_source_hash = None
        await db.commit()
        pack = ContextPack()
        await ContextPackBuilder(db=db)._build_proximity(pack, r.project, v2, 1)
        text = '\n'.join(pack.recent_summaries)
        assert '上一卷末，负伤过桥' not in text
        assert '大纲规划冒充事实' not in text
        assert 'CH-100' in text and '摘要缺失或与当前稿件不匹配' in text
        current = await db.scalar(select(Chapter).where(Chapter.volume_id == v2))
        neighbors = await load_evaluation_neighbors(db, current.id, r.project)
        assert '上一卷末，负伤过桥' not in neighbors.previous_context
        assert '大纲规划冒充事实' not in neighbors.previous_context
        assert previous.content_text in neighbors.previous_context


@pytest.mark.asyncio
async def test_unverified_summary_is_not_injected_by_legacy_memory(runtime_db):
    async with runtime_db.sessions() as db:
        previous = await db.get(Chapter, runtime_db.chapter)
        previous.summary = '未验证的旧摘要'
        current = Chapter(volume_id=previous.volume_id, chapter_idx=2, title='下一章')
        db.add(current)
        await db.commit()
        memory = HierarchicalMemory(db)
        text = await memory._gather_chapter_summaries(str(runtime_db.project), str(previous.volume_id), 2)
        assert '未验证的旧摘要' not in text
        assert '摘要缺失或来源失效' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['legacy', 'draft', 'missing', 'valid'])
async def test_volume_summary_does_not_skip_unverified_or_missing_chapters(runtime_db, monkeypatch, problem):
    from types import SimpleNamespace
    import app.services.memory as module
    generate = AsyncMock(return_value=SimpleNamespace(text='整卷摘要'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    async with runtime_db.sessions() as db:
        chapter = await db.get(Chapter, runtime_db.chapter)
        chapter.summary_source_hash = summary_source_hash(chapter.title, chapter.content_text, chapter.summary)
        chapter.status = 'completed'
        chapter.global_idx = 1
        if problem == 'legacy':
            chapter.summary_source_hash = None
        if problem == 'draft':
            chapter.status = 'needs_review'
        if problem == 'missing':
            db.add(Chapter(volume_id=chapter.volume_id, chapter_idx=2, title='缺摘要', status='completed'))
        await db.commit()
        result = await HierarchicalMemory(db).generate_volume_summary(str(chapter.volume_id))
        assert result == ('整卷摘要' if problem == 'valid' else '')
        assert generate.await_count == int(problem == 'valid')
