from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import versions
from app.services.chapter_evaluator import EvaluationResult
from app.services.evaluation_neighbors import EvaluationNeighbors
from app.services.evaluation_source import chapter_evaluation_source


@pytest.mark.asyncio
@pytest.mark.parametrize('deleted', [False, True])
async def test_manual_evaluation_releases_read_transaction_and_preserves_original_inputs(monkeypatch, deleted):
    chapter = NS(id='c', volume_id='v', content_text='评审前正文。', outline_json={'nested': {'goal': '旧目标'}})
    calls = []
    async def get_chapter(cid, db):
        calls.append('chapter')
        if deleted and calls.count('chapter') == 2:
            raise HTTPException(status_code=404, detail='Chapter not found')
        return chapter
    monkeypatch.setattr(versions, '_get_chapter_or_404', get_chapter)
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value={}))
    monkeypatch.setattr('app.services.character_cognition.serialize_for_prompt', lambda ledger, **kwargs: '认知记录')
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors', AsyncMock(return_value=EvaluationNeighbors('前文', '后文')))
    saved = []
    async def rollback():
        calls.append('rollback')
        chapter.content_text = '作者新正文。'
        chapter.outline_json['nested']['goal'] = '作者新目标'
    async def flush():
        calls.append('flush')
        saved[0].id = 'e'
    db = NS(get=AsyncMock(return_value=NS(project_id='p')), rollback=AsyncMock(side_effect=rollback),
            add=saved.append, flush=AsyncMock(side_effect=flush), refresh=AsyncMock())
    async def evaluate(**kwargs):
        calls.append('model')
        assert calls == ['chapter', 'rollback', 'model']
        assert kwargs['chapter_text'] == '评审前正文。'
        assert kwargs['chapter_outline'] == {'nested': {'goal': '旧目标'}}
        assert kwargs['project_id'] == 'p' and kwargs['chapter_id'] == 'c'
        return EvaluationResult(overall=8, chapter_source=chapter_evaluation_source(
            'c', kwargs['chapter_text'], kwargs['chapter_outline']))
    monkeypatch.setattr(versions.ChapterEvaluator, 'evaluate', AsyncMock(side_effect=evaluate))
    if deleted:
        with pytest.raises(HTTPException) as exc:
            await versions.evaluate_chapter('c', versions.EvaluateRequest(), db)
        assert exc.value.status_code == 404 and not saved
    else:
        response = await versions.evaluate_chapter('c', versions.EvaluateRequest(), db)
        assert response.chapter_source == saved[0].source_json
        assert response.chapter_source != chapter_evaluation_source('c', chapter.content_text, chapter.outline_json)
        assert calls == ['chapter', 'rollback', 'model', 'chapter', 'flush']
    assert chapter.content_text == '作者新正文。'


@pytest.mark.asyncio
async def test_manual_cognition_failure_blocks_scoring_with_safe_error(monkeypatch):
    chapter = NS(id='c', volume_id='v', content_text='当前正文。', outline_json={})
    monkeypatch.setattr(versions, '_get_chapter_or_404', AsyncMock(return_value=chapter))
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(side_effect=RuntimeError('private diagnostic')))
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors', AsyncMock(return_value=EvaluationNeighbors('前文', '后文')))
    model = AsyncMock(return_value=EvaluationResult(overall=9))
    monkeypatch.setattr(versions.ChapterEvaluator, 'evaluate', model)
    db = NS(get=AsyncMock(return_value=NS(project_id='p')), rollback=AsyncMock(), add=lambda row: None,
            flush=AsyncMock(), refresh=AsyncMock())
    with pytest.raises(HTTPException) as caught:
        await versions.evaluate_chapter('c', versions.EvaluateRequest(), db)
    assert caught.value.status_code == 503 and 'private' not in str(caught.value.detail)
    model.assert_not_awaited()
    db.rollback.assert_awaited_once()
