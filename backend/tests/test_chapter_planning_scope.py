import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Outline, Project, Volume
from app.services.outline_contract import chapter_scope_directive
from app.services.outline_generator import OutlineGenerator
from app.services import chapter_outline_expander as expander, prompt_registry


def book():
    return {'raw_text': '菜谱属于母亲；旧书暂存周砚住处。',
            'structured': {'characters': [{'name': '林禾', 'relationship': '周砚的学徒'}]},
            'volume_plan': [{'idx': 1, 'est_chapters': 3}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_both_generator_modes_preserve_book_facts_and_chapter_budget(stream):
    captured = []
    async def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(text='{"summary":"完成交接"}')
    async def generate_stream(**kwargs):
        captured.append(kwargs)
        yield '{"summary":"完成交接"}'
    obj = OutlineGenerator()
    obj.router = SimpleNamespace(generate=generate, generate_stream=generate_stream)
    result = await obj.generate_chapter_outline(book(), {'volume_idx': 1, 'chapter_count': 3}, 3,
                                                stream=stream, target_word_count=1800)
    if stream:
        assert ''.join([chunk async for chunk in result])
    prompt = captured[0]['messages'][1]['content']
    assert all(fact in prompt for fact in ('菜谱属于母亲', '周砚的学徒', '旧书暂存周砚住处', '正文目标 1800 字', '全书已规划的最后一章'))
    assert 'word_count_target 必须为 1800' in prompt


@pytest.mark.parametrize('plan,volume,idx', [
    ([{'idx': 1}, {'idx': 2}], {'volume_idx': 1, 'chapter_count': 3}, 3),
    ([{'idx': 1}], {'volume_idx': 1, 'chapter_count': 3}, 2),
    ([], {'volume_idx': 1, 'chapter_count': 3}, 3),
    ([{'idx': 2}], {'volume_idx': 2, 'chapter_count': 3}, 3),
    ([{'idx': 1, 'est_chapters': 10}], {'volume_idx': 1, 'chapter_count': 3}, 3),
])
def test_last_stored_chapter_or_volume_is_not_automatically_book_ending(plan, volume, idx):
    assert '全书已规划的最后一章' not in chapter_scope_directive({'volume_plan': plan}, volume, idx)


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_actual_expansion_carries_book_scope_and_persists_effective_budget(runtime_db, monkeypatch, legacy):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Outline.__table__.create(c, checkfirst=True))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.chapter_idx = 3
        chapter.target_word_count = 50000 if legacy else 1800
        chapter.outline_json = {'summary': '按既定计划关店', 'key_events': ['带走菜谱']}
        volume = await db.get(Volume, chapter.volume_id)
        volume.volume_idx = 1
        project = await db.get(Project, r.project)
        project.settings_json = {'target_chapter_words': 1800}
        db.add_all([Outline(project_id=r.project, level='book', is_confirmed=1, content_json=book()),
                    Outline(project_id=r.project, level='volume', is_confirmed=1,
                            content_json={'volume_id': str(volume.id), 'volume_idx': 1, 'chapter_count': 3})])
        await db.commit()
    async def model(task_type, user_content, db, extra_system, **kwargs):
        assert all(value in user_content for value in ('菜谱属于母亲', '周砚的学徒', '正文目标 1800 字', '全书已规划的最后一章'))
        return SimpleNamespace(text=json.dumps({'summary': '林禾带走菜谱，向母亲说明安排。',
                                               'key_events': ['带走菜谱'], 'word_count_target': 9000,
                                               'next_chapter_hook': '全书完结，旧店关闭。'}))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', model)
    async with r.sessions() as db:
        result = await expander.expand_chapter_outline(str(r.project), str(r.chapter), db)
        await db.commit()
        assert result['word_count_target'] == 1800
        assert result['next_chapter_hook'] == '全书完结，旧店关闭。'
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert ch.outline_json['word_count_target'] == 1800
        assert ch.content_text == '原稿。'


@pytest.mark.asyncio
async def test_sse_rejects_ambiguous_chapter_budget_before_model_call(runtime_db):
    from fastapi import HTTPException
    from app.api.generate import GenerateOutlineRequest, generate_outline
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Outline.__table__.create(c, checkfirst=True))
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        outline = Outline(project_id=r.project, level='volume',
                          content_json={'volume_id': str(chapter.volume_id), 'volume_idx': 1})
        db.add(outline)
        db.add(Chapter(volume_id=chapter.volume_id, chapter_idx=chapter.chapter_idx,
                       title='重复章号', target_word_count=9000))
        await db.commit()
        with pytest.raises(HTTPException) as caught:
            await generate_outline(GenerateOutlineRequest(project_id=str(r.project), level='chapter',
                                                           parent_outline_id=str(outline.id), chapter_idx=1), db=db)
        assert caught.value.status_code == 409
        assert '章号重复' in caught.value.detail
