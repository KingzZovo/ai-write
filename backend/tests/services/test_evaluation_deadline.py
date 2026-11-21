import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services import chapter_evaluator as evaluator
from app.services.chapter_acceptance import evaluation_passes
from app.services.auto_revise import should_revise
from app.services.model_router import OpenAIProvider


@pytest.mark.parametrize('bad', [True, 0, -1, float('nan'), float('inf'), 'bad'])
def test_bad_total_budget_rejected(bad):
    with pytest.raises(ValueError):
        evaluator.evaluation_timeouts(bad)


def test_shared_default_and_explicit_override(monkeypatch):
    monkeypatch.setattr(evaluator.settings, 'EVALUATION_TOTAL_TIMEOUT_SECONDS', 180)
    monkeypatch.setattr(evaluator.settings, 'EVALUATION_REQUEST_TIMEOUT', 120)
    assert evaluator.evaluation_timeouts() == (180, 120)
    assert evaluator.evaluation_timeouts(30) == (30, 30)
    assert evaluator.evaluation_timeouts(240) == (240, 120)


@pytest.mark.asyncio
async def test_deadline_cancels_delivery_and_returns_untrusted_verdict(monkeypatch):
    closed = asyncio.Event()
    async def delivery(**kwargs):
        assert kwargs['request_timeout'] == .02
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(evaluator, 'run_text_prompt', delivery)
    result = await evaluator.ChapterEvaluator().evaluate('正文。', {}, timeout_seconds=.02)
    assert closed.is_set()
    assert result.parse_failed
    assert not evaluation_passes(result)
    assert not should_revise(result)
    assert 'timed out' in result.issues[0]['description']


@pytest.mark.asyncio
async def test_external_cancellation_propagates_instead_of_becoming_a_verdict(monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    async def delivery(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(evaluator, 'run_text_prompt', delivery)
    task = asyncio.create_task(evaluator.ChapterEvaluator().evaluate('正文。', {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_completed_judgement_survives_new_default(monkeypatch):
    data = {d: {'score': 9, 'issues': []} for d in (
        'plot_coherence','character_consistency','style_adherence','narrative_pacing','foreshadow_handling')}
    call = AsyncMock(return_value=SimpleNamespace(text=json.dumps(data), usage=SimpleNamespace(total_tokens=1)))
    monkeypatch.setattr(evaluator, 'run_text_prompt', call)
    result = await evaluator.ChapterEvaluator().evaluate('正文。', {})
    assert evaluation_passes(result)
    assert call.await_args.kwargs['request_timeout'] == min(evaluator.settings.EVALUATION_TOTAL_TIMEOUT_SECONDS, evaluator.settings.EVALUATION_REQUEST_TIMEOUT)


@pytest.mark.asyncio
async def test_internal_stream_is_closed_on_evaluation_cancellation(monkeypatch):
    from app.services import model_router as router
    stream = SimpleNamespace()
    class StalledStream:
        def __aiter__(self): return self
        async def __anext__(self):
            await asyncio.Event().wait()
        close = AsyncMock()
    stream = StalledStream()
    provider = OpenAIProvider(api_key='test')
    monkeypatch.setattr(router, '_should_force_internal_stream_for_model', lambda _: True)
    monkeypatch.setattr(router, '_wait_openai_compat_turn', AsyncMock())
    create = AsyncMock(return_value=stream)
    monkeypatch.setattr(provider, '_create_chat_stream', create)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.02):
            await provider.generate(messages=[], model='test', task_type='chapter_evaluation')
    stream.close.assert_awaited_once()
    create.assert_awaited_once()
