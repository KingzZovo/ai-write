import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services import chapter_evaluator as evaluator
from app.services.evaluation_source import chapter_evaluation_source


@pytest.mark.asyncio
async def test_evaluator_captures_input_before_model_call_mutates_outline(monkeypatch):
    outline = {'summary': '旧细纲'}
    original = chapter_evaluation_source('c', '旧正文。', outline)
    data = {d: {'score': 8, 'issues': []} for d in ('plot_coherence', 'character_consistency', 'style_adherence', 'narrative_pacing', 'foreshadow_handling')}
    async def model(**kwargs):
        outline['summary'] = '作者新细纲'
        return NS(text=json.dumps(data), usage=NS(total_tokens=1), finish_reason='stop')
    monkeypatch.setattr(evaluator, 'run_text_prompt', model)
    result = await evaluator.ChapterEvaluator().evaluate('旧正文。', outline, chapter_id='c')
    assert result.chapter_source == original
    assert result.to_dict()['chapter_source'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('source_kind', ['current', 'changed', 'legacy'])
async def test_latest_saved_evaluation_checks_persisted_source(source_kind):
    from app.api.evaluate import latest_chapter_evaluation
    ch = NS(id='c', content_text='旧正文。', outline_json={})
    source = None if source_kind == 'legacy' else chapter_evaluation_source(ch.id, ch.content_text, ch.outline_json)
    if source_kind == 'changed': ch.content_text = '新正文。'
    row = NS(id='e', created_at=None, source_json=source, issues_json=[], **{d: 8 for d in (
        'overall', 'plot_coherence', 'character_consistency', 'style_adherence', 'narrative_pacing', 'foreshadow_handling')})
    db = NS(get=AsyncMock(return_value=ch), scalar=AsyncMock(side_effect=[None, row]))
    response = await latest_chapter_evaluation('c', db)
    assert response.source_status == {'current': 'unchanged', 'changed': 'changed', 'legacy': 'unknown'}[source_kind]
    assert response.result['chapter_source'] == source


def test_all_evaluation_persistence_sites_copy_the_evaluated_source():
    for name, count in [('app/api/generate.py', 2), ('app/tasks/generation_tasks.py', 1), ('app/api/versions.py', 1)]:
        tree = ast.parse((Path(__file__).parents[1] / name).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'ChapterEvaluation']
        assert len(calls) == count
        for call in calls:
            source = next(k.value for k in call.keywords if k.arg == 'source_json')
            assert isinstance(source, ast.Call) and source.func.id == 'getattr'
            assert source.args[1].value == 'chapter_source'


def test_source_column_is_nullable_for_unverifiable_history():
    from app.models.project import ChapterEvaluation
    assert ChapterEvaluation.__table__.c.source_json.nullable
