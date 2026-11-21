from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.api import chapters as api
from app.models.project import Chapter


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['same', 'stale', 'force', 'legacy'])
async def test_chapter_outline_save_compares_before_any_mutation(mode):
    chapter = Chapter(id=uuid4(), volume_id=uuid4(), chapter_idx=1, title='作者章名',
        outline_json={'summary': '最新细纲'}, content_text='作者正文', status='draft', word_count=4,
        target_word_count=1800)
    before = deepcopy(chapter.outline_json)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one_or_none=lambda: chapter),
        SimpleNamespace(scalar_one_or_none=lambda: None)]), flush=AsyncMock(), refresh=AsyncMock())
    expected = None if mode == 'legacy' else ({'summary': '旧细纲'} if mode in ('stale', 'force') else before)
    body = api.ChapterUpdate(outline_json={'summary': '作者修订'}, expected_outline_json=expected,
        title='新标题', force=mode == 'force')
    if mode in ('stale', 'force'):
        with pytest.raises(HTTPException) as exc: await api.update_chapter(str(uuid4()), str(chapter.id), body, db)
        assert exc.value.status_code == 409
        assert chapter.outline_json == before and chapter.title == '作者章名'
        db.flush.assert_not_awaited()
    else:
        result = await api.update_chapter(str(uuid4()), str(chapter.id), body, db)
        assert result.outline_json == {'summary': '作者修订', 'chapter_idx': 1,
            'chapter_id': str(chapter.id), 'volume_id': str(chapter.volume_id)}
    assert chapter.content_text == '作者正文'
    query = db.execute.call_args_list[0].args[0]
    assert (query._for_update_arg is not None) == (mode != 'legacy')
