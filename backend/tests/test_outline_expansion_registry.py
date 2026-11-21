"""Expansion -> Registry -> validation -> write -> context with real SQL."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Outline
from app.services import chapter_outline_expander as expander, prompt_registry
from app.services.context_pack import ContextPack


@pytest_asyncio.fixture(autouse=True)
async def outline_table(runtime_db):
    async with runtime_db.engine.begin() as connection:
        await connection.run_sync(lambda conn: Outline.__table__.create(conn, checkfirst=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_edit', [False, True])
async def test_expansion_uses_registry_and_preserves_concurrent_author_edit(runtime_db, monkeypatch, manual_edit):
    r = runtime_db
    stub = {'summary': '原有细纲。', 'key_events': ['准备出发'], 'foreshadows_planted': [{'description': '未兑现承诺'}]}
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.outline_json = stub
        await db.commit()
    context = {'project_id': str(r.project), 'book_outline': {}, 'volume_outline': {}, 'prev_outline': {},
               'prev_text': '', 'stub': stub}
    monkeypatch.setattr(expander, '_gather_context', AsyncMock(return_value=context))
    calls = []
    async def model(task_type, user_content, db, extra_system, **kwargs):
        calls.append((task_type, kwargs))
        assert '世界逻辑合同' in extra_system
        assert kwargs['project_id'] == str(r.project) and kwargs['chapter_id'] == str(r.chapter)
        if manual_edit:
            async with r.sessions() as other:
                chapter = await other.get(Chapter, r.chapter)
                chapter.outline_json = {'summary': '作者刚修改的版本。'}
                await other.commit()
        return SimpleNamespace(text=json.dumps({'summary': '完成渡河后把信交给守卫。', 'key_events': ['渡河', '交信'],
            'entity_transfers': [{'item': '密信', 'to': '守卫'}], 'action_budget': '一刻钟内渡河'}))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', model)
    async with r.sessions() as db:
        if manual_edit:
            with pytest.raises(expander.ChapterOutlineExpandError, match='已被修改'):
                await expander.expand_chapter_outline(str(r.project), str(r.chapter), db)
            await db.rollback()
        else:
            result = await expander.expand_chapter_outline(str(r.project), str(r.chapter), db)
            await db.commit()
            prompt = ContextPack(current_outline=result).to_system_prompt(token_budget=16000)
            assert all(value in prompt for value in ('密信', '一刻钟内渡河', '未兑现承诺'))
        assert calls[0][0] == 'outline_chapter'
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert ch.content_text == '原稿。'
        assert ch.outline_json['summary'] == ('作者刚修改的版本。' if manual_edit else '完成渡河后把信交给守卫。')


@pytest.mark.asyncio
async def test_expansion_rechecks_volume_ownership_before_persist(runtime_db, monkeypatch):
    from app.models.project import Project, Volume
    r = runtime_db
    context = {'project_id': str(r.project), 'book_outline': {}, 'volume_outline': {}, 'prev_outline': {},
               'prev_text': '', 'stub': {}}
    monkeypatch.setattr(expander, '_gather_context', AsyncMock(return_value=context))
    async def model(*args, **kwargs):
        async with r.sessions() as db:
            other = Project(title='另一本书')
            db.add(other)
            await db.flush()
            chapter = await db.get(Chapter, r.chapter)
            volume = await db.get(Volume, chapter.volume_id)
            volume.project_id = other.id
            await db.commit()
        return SimpleNamespace(text='{"summary":"新细纲。","key_events":["新事件"]}')
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', model)
    async with r.sessions() as db:
        with pytest.raises(expander.ChapterOutlineExpandError, match='所属作品已变更'):
            await expander.expand_chapter_outline(str(r.project), str(r.chapter), db)
        await db.rollback()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).outline_json == {}
