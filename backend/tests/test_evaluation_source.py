from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.evaluation_source import chapter_evaluation_source, evaluation_source_status


def chapter(): return NS(id='c', content_text='原文。\n保留换行。', outline_json={'b': 2, 'a': 1})


def test_source_uses_exact_text_identity_and_semantic_json_key_order():
    ch = chapter()
    source = chapter_evaluation_source(ch.id, ch.content_text, {'a': 1, 'b': 2})
    result = {'chapter_source': source}
    assert evaluation_source_status(result, ch) == 'unchanged'
    assert '原文' not in str(source)
    ch.content_text += ' '
    assert evaluation_source_status(result, ch) == 'changed'


@pytest.mark.parametrize('field,value', [('id', 'other'), ('outline_json', {'a': 2})])
def test_changed_identity_or_outline_is_not_current(field, value):
    ch = chapter()
    result = {'chapter_source': chapter_evaluation_source(ch.id, ch.content_text, ch.outline_json)}
    setattr(ch, field, value)
    assert evaluation_source_status(result, ch) == 'changed'
    assert evaluation_source_status(result, None) == 'changed'


@pytest.mark.parametrize('source', [None, {}, {'version': 2}, {'version': True}, {'version': 1, 'chapter_id': 'c', 'sha256': 'invalid'}])
def test_legacy_and_malformed_sources_remain_unknown(source):
    assert evaluation_source_status({'chapter_source': source}, chapter()) == 'unknown'


@pytest.mark.asyncio
async def test_status_read_rechecks_current_chapter_without_modifying_historical_result():
    from app.api.evaluate import get_evaluate_task
    from app.models.project import Chapter, EvaluateTask
    ch = chapter()
    result = {'overall': 9, 'chapter_source': chapter_evaluation_source(ch.id, ch.content_text, ch.outline_json)}
    row = NS(id='t', chapter_id='c', status='completed', result_json=result, round_idx=0, caller='test',
             error_text=None, started_at=None, completed_at=None, created_at=None, updated_at=None)
    db = NS(get=AsyncMock(side_effect=lambda model, key: row if model is EvaluateTask else ch))
    assert (await get_evaluate_task('t', db)).source_status == 'unchanged'
    ch.outline_json = {'new': 'outline'}
    response = await get_evaluate_task('t', db)
    assert response.source_status == 'changed' and response.result == result
    assert (Chapter, 'c') in [call.args for call in db.get.call_args_list]
