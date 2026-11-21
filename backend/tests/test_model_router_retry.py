"""retry_attempts kwarg must control call_with_retry attempts (was silently ignored).

Callers thread `retry_attempts` through llm_kwargs (chapter_generator.py,
api/generate.py via SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS, knowledge_tasks.py via
SINGLE_SHOT_LLM_RETRY_ATTEMPTS), but OpenAIProvider.generate hardcoded
`attempts=1 if task_type == "evaluation" else 4`, so the configuration had no
effect. With request_timeout=840s on the single-shot path, 4 forced attempts
could hang a request for up to 56 minutes.
"""
import pytest
from types import SimpleNamespace
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
    # No retry_attempts passed: non-stream/evaluation calls stay single-shot;
    # streaming generation keeps the broader retry envelope.
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
    assert captured["attempts"] == 1

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

    captured = {}
    with patch(
        "app.services.llm_retry.call_with_retry",
        side_effect=_fake_retry(captured),
    ):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="chapter", stream=True,
        )
    assert captured["attempts"] == 4


@pytest.mark.asyncio
async def test_non_positive_retry_attempts_falls_back_to_default():
    # retry_attempts=0 (or negative) is not a valid attempt count: keep the
    # resolved default for this call mode.
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
    assert captured["attempts"] == 1


# ---- B3: evaluation timeout must be env-configurable (45s -> default 120s) ----
# Evaluation calls read ~13K chars of Chinese prose; the hardcoded 45s default
# timed out on slow endpoints, surfacing as all-zero EvaluationResult scores.


def test_evaluation_timeout_env_override(monkeypatch):
    monkeypatch.setenv("EVALUATION_REQUEST_TIMEOUT", "200")
    from app.services.model_router import _resolve_nonstream_timeout
    assert _resolve_nonstream_timeout(None, "evaluation") == 200
    assert _resolve_nonstream_timeout(30, "evaluation") == 30      # 显式传参优先
    assert _resolve_nonstream_timeout(None, "chapter") == 45       # 非评估保持 45


def test_evaluation_timeout_default_120(monkeypatch):
    monkeypatch.delenv("EVALUATION_REQUEST_TIMEOUT", raising=False)
    from app.services.model_router import _resolve_nonstream_timeout
    assert _resolve_nonstream_timeout(None, "evaluation") == 120


@pytest.mark.asyncio
async def test_nonstream_timeout_wired_into_api_call(monkeypatch):
    # Wiring check: generate() must pass the resolved timeout through to the
    # actual chat.completions.create call (not just define the helper).
    monkeypatch.setenv("EVALUATION_REQUEST_TIMEOUT", "321")
    provider = OpenAIProvider(api_key="test")
    captured: dict = {}

    async def fake_create(**kwargs):
        captured["timeout"] = kwargs.get("timeout")
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)], usage=None
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    await provider.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="m", task_type="evaluation", stream=False,
    )
    assert captured["timeout"] == 321
