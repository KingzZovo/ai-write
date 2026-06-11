"""retry_attempts kwarg must control call_with_retry attempts (was silently ignored).

Callers thread `retry_attempts` through llm_kwargs (chapter_generator.py,
api/generate.py via SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS, knowledge_tasks.py via
SINGLE_SHOT_LLM_RETRY_ATTEMPTS), but OpenAIProvider.generate hardcoded
`attempts=1 if task_type == "evaluation" else 4`, so the configuration had no
effect. With request_timeout=840s on the single-shot path, 4 forced attempts
could hang a request for up to 56 minutes.
"""
import pytest
from unittest.mock import patch

from app.services.model_router import GenerationResult, OpenAIProvider, TokenUsage


def _fake_retry(captured: dict):
    async def fake_call_with_retry(fn, label="", attempts=4, **kwargs):
        captured["attempts"] = attempts
        return GenerationResult(
            text="ok", usage=TokenUsage(), model="m", provider="openai"
        )

    return fake_call_with_retry


@pytest.mark.asyncio
async def test_retry_attempts_kwarg_is_honored():
    provider = OpenAIProvider(api_key="test")
    captured: dict = {}

    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="chapter", retry_attempts=2, stream=False,
        )
    assert captured["attempts"] == 2


@pytest.mark.asyncio
async def test_retry_attempts_kwarg_honored_for_evaluation():
    # Even evaluation (default 1) must honor an explicit retry_attempts.
    provider = OpenAIProvider(api_key="test")
    captured: dict = {}

    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="evaluation", retry_attempts=3, stream=False,
        )
    assert captured["attempts"] == 3


@pytest.mark.asyncio
async def test_default_attempts_unchanged_without_kwarg():
    # No retry_attempts passed: evaluation=1, everything else=4 (status quo).
    provider = OpenAIProvider(api_key="test")

    captured: dict = {}
    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="chapter", stream=False,
        )
    assert captured["attempts"] == 4

    captured = {}
    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="evaluation", stream=False,
        )
    assert captured["attempts"] == 1


@pytest.mark.asyncio
async def test_non_positive_retry_attempts_falls_back_to_default():
    # retry_attempts=0 (or negative) is not a valid attempt count: keep default.
    provider = OpenAIProvider(api_key="test")
    captured: dict = {}

    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="chapter", retry_attempts=0, stream=False,
        )
    assert captured["attempts"] == 4
