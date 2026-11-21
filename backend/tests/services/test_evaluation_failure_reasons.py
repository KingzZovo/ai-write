import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import chapter_evaluator as evaluator
from app.services.auto_revise import should_revise
from app.services.chapter_acceptance import evaluation_passes


@pytest.mark.asyncio
@pytest.mark.parametrize('response,reason', [
    (RuntimeError('secret-provider-token'), 'call_failed'),
    (json.JSONDecodeError('secret-provider-token', 'x', 0), 'call_failed'),
    (TimeoutError('secret-provider-token'), 'timeout'),
    ('not JSON secret-provider-token', 'parse_failed'),
    ('[]', 'parse_failed'),
    ('```', 'parse_failed'),
    ('{}', 'parse_failed'),
])
async def test_failures_are_classified_without_provider_details(monkeypatch, response, reason):
    call = AsyncMock()
    if isinstance(response, Exception):
        call.side_effect = response
    else:
        call.return_value = SimpleNamespace(text=response, usage=SimpleNamespace(total_tokens=1))
    monkeypatch.setattr(evaluator, 'run_text_prompt', call)
    result = await evaluator.ChapterEvaluator().evaluate('正文。', {})
    assert result.parse_failed
    assert result.failure_reason == reason
    assert result.issues[0]['failure_reason'] == reason
    assert 'secret-provider-token' not in json.dumps(result.to_dict())
    assert not should_revise(result)
    assert not evaluation_passes(result)


@pytest.mark.asyncio
async def test_empty_chapter_is_not_a_provider_or_parse_error(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(evaluator, 'run_text_prompt', call)
    result = await evaluator.ChapterEvaluator().evaluate('  ', {})
    assert result.failure_reason == 'empty_input'
    call.assert_not_awaited()
    assert not should_revise(result)
    assert not evaluation_passes(result)
