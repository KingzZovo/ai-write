"""Offline checks supplement, but do not replace, the PostgreSQL race tests."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import chapter_outline_expander as expander, prompt_registry


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'replacement_context', 'shared_dict_mutation'])
async def test_expansion_freezes_source_and_refuses_changed_context(monkeypatch, change):
    chapter = SimpleNamespace(id='chapter', volume_id='volume', chapter_idx=1, title='作者标题',
        updated_at='version-1', target_word_count=1800, outline_json={'summary': '作者旧细纲'}, content_text='作者正文')
    project = SimpleNamespace(id='project', deleted_at=None)
    volume = SimpleNamespace(id='volume', project_id='project')
    db = SimpleNamespace(get=AsyncMock(return_value=chapter),
        scalar=AsyncMock(side_effect=[project, volume, chapter]), execute=AsyncMock(),
        rollback=AsyncMock(), flush=AsyncMock(), refresh=AsyncMock())
    context = {'project_id': 'project', 'book_outline': {'raw_text': '原书纲'},
        'volume_outline': {}, 'prev_outline': {}, 'prev_text': '', 'target_word_count': 1800,
        'stub': {'summary': '作者旧细纲', 'key_events': ['原安排']}, 'active_foreshadows': []}
    current = deepcopy(context)
    gathered = 0
    async def gather(*args, **kwargs):
        nonlocal gathered
        gathered += 1
        if gathered == 1: return context
        assert kwargs['lock'] is True
        return context if change == 'shared_dict_mutation' else current
    async def generate(*args, **kwargs):
        assert db.rollback.await_count == 1
        if change == 'replacement_context': current['book_outline']['raw_text'] = '作者新书纲'
        if change == 'shared_dict_mutation': context['book_outline']['raw_text'] = '作者新书纲'
        return {'summary': '本章完整过程。', 'key_events': ['交付信件']}
    monkeypatch.setattr(expander, '_gather_context', gather)
    monkeypatch.setattr(prompt_registry, 'run_structured_prompt', generate)
    if change == 'none':
        result = await expander.expand_chapter_outline('project', 'chapter', db)
        assert result['summary'] == '本章完整过程。'
        assert result['title'] == '作者标题' and result['word_count_target'] == 1800
        db.flush.assert_awaited_once()
    else:
        with pytest.raises(expander.ChapterOutlineExpandError, match='已修改'):
            await expander.expand_chapter_outline('project', 'chapter', db)
        assert chapter.outline_json == {'summary': '作者旧细纲'}
        db.flush.assert_not_awaited()
    assert chapter.content_text == '作者正文'
