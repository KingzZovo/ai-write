import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import chapter_evaluator as evaluator
from app.services.auto_revise import should_revise
from app.services.chapter_acceptance import evaluation_passes


def payload(score):
    return json.dumps({d: {'score': score, 'issues': []} for d in (
        'plot_coherence', 'character_consistency', 'style_adherence', 'narrative_pacing', 'foreshadow_handling')})


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter', 'tool_calls', 'tool_use', 'unknown'])
@pytest.mark.parametrize('score', [2, 9])
async def test_incomplete_even_parseable_verdict_cannot_trigger_rewrite_or_acceptance(monkeypatch, reason, score):
    call = AsyncMock(return_value=NS(text=payload(score), finish_reason=reason, usage=NS(total_tokens=1)))
    parse = Mock(side_effect=AssertionError('Incomplete response must not be parsed'))
    monkeypatch.setattr(evaluator, 'run_text_prompt', call)
    monkeypatch.setattr(evaluator, '_parse_evaluation_response', parse)
    result = await evaluator.ChapterEvaluator().evaluate('原始正文保持。', {})
    assert result.parse_failed and result.failure_reason == 'incomplete_response'
    assert not should_revise(result) and not evaluation_passes(result)
    assert result.issues[0]['dimension'] == 'system'
    parse.assert_not_called()
    call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'end_turn', 'stop_sequence', None])
async def test_normal_or_unknown_legacy_response_still_requires_valid_schema(monkeypatch, reason):
    call = AsyncMock(return_value=NS(text=payload(9), finish_reason=reason, usage=NS(total_tokens=1)))
    monkeypatch.setattr(evaluator, 'run_text_prompt', call)
    result = await evaluator.ChapterEvaluator().evaluate('原始正文保持。', {})
    assert evaluation_passes(result)
    call.return_value.text = '{}'
    invalid = await evaluator.ChapterEvaluator().evaluate('原始正文保持。', {})
    assert invalid.parse_failed and not evaluation_passes(invalid)
