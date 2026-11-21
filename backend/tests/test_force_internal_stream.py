"""Relay thinking-burn workaround: force internal streaming for matching models.

E2E evidence (2026-07-26): non-streaming completions to relay claude-* return
HTTP 200 with EMPTY text and ``output_tokens == max_tokens`` — the relay burns
the whole output budget on hidden thinking. Streaming the identical request
works. Affected callers force stream=False (drafter/rewrite via
``retry_attempts=1, stream=False``; evaluation via its task_type special case),
so every chapter degraded: evaluator JSON parse failed → all-zero scores,
rewrites came back empty → chapters permanently blocked.

Fix under test:
1. Models matching ``settings.LLM_FORCE_INTERNAL_STREAM_MODELS`` (comma
   separated substrings, same style as LLM_MERGE_SYSTEM_INTO_USER_MODELS)
   route non-streaming ``OpenAIProvider.generate`` calls through the
   internally-streamed branch, assembling full text before returning.
2. Empty text + output budget exhausted raises ``LLMEmptyCompletionError``
   (retryable) instead of returning success-with-empty.
3. Forced-internal-stream calls get an attempts floor of 2 so evaluation's
   attempts=1 (and drafter's explicit retry_attempts=1) can't defeat the fix.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.model_router as mr
from app.services.llm_retry import LLMEmptyCompletionError, _is_retryable
from app.services.model_router import (
    GenerationResult,
    OpenAIProvider,
    TokenUsage,
    _should_force_internal_stream_for_model,
)


async def _fast_sleep(_delay):
    return None


class _Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = ([SimpleNamespace(delta=SimpleNamespace(content=content))]
                        if content is not None else [])
        self.usage = usage


def _usage_chunk(prompt=11, completion=7):
    return _Chunk(usage=SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion, prompt_tokens_details=None))


def _stream_client(calls: list[dict], responses: list[list[_Chunk]]):
    """Fake AsyncOpenAI client: call N returns the Nth chunk list as a stream."""

    async def create(**kwargs):
        calls.append(kwargs)
        chunks = responses[min(len(calls) - 1, len(responses) - 1)]

        async def _gen():
            for c in chunks:
                yield c
        return _gen()

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def _nonstream_client(calls: list[dict], responses: list):
    async def create(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def _nonstream_resp(content, prompt=10, completion=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                              total_tokens=prompt + completion),
    )


@pytest.fixture(autouse=True)
def _force_claude(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_FORCE_INTERNAL_STREAM_MODELS", "claude-")
    monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "")


class TestMatching:
    def test_substring_match(self):
        assert _should_force_internal_stream_for_model("claude-sonnet-5") is True
        assert _should_force_internal_stream_for_model("claude-opus-5") is True
        assert _should_force_internal_stream_for_model("gpt-4o") is False

    def test_empty_setting_disables(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_FORCE_INTERNAL_STREAM_MODELS", "")
        assert _should_force_internal_stream_for_model("claude-sonnet-5") is False

    def test_multiple_patterns(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(
            settings, "LLM_FORCE_INTERNAL_STREAM_MODELS", "claude-, grok")
        assert _should_force_internal_stream_for_model("grok-4") is True
        assert _should_force_internal_stream_for_model("gemini-3.1-pro") is False


class TestForcedInternalStream:
    @pytest.mark.asyncio
    async def test_nonstream_caller_gets_streamed_assembly_with_usage(self):
        # stream=False + matching model: upstream request must be streaming,
        # full text assembled, usage recorded via the include_usage probe.
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _stream_client(calls, [
            [_Chunk("第一"), _Chunk("场。"), _usage_chunk(prompt=42, completion=9)],
        ])
        result = await provider.generate(
            [{"role": "user", "content": "写"}],
            model="claude-sonnet-5", task_type="chapter", stream=False,
        )
        assert result.text == "第一场。"
        assert result.usage.input_tokens == 42
        assert result.usage.output_tokens == 9
        assert len(calls) == 1
        assert calls[0].get("stream") is True
        # non-stream timeout semantics preserved (45s default for chapter)
        assert calls[0].get("timeout") == 45

    @pytest.mark.asyncio
    async def test_evaluation_gets_streamed_path(self):
        # evaluation historically always took the true non-stream branch;
        # for matching models it must now stream internally too.
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _stream_client(calls, [
            [_Chunk('{"overall": 8}'), _usage_chunk()],
        ])
        result = await provider.generate(
            [{"role": "user", "content": "评分"}],
            model="claude-sonnet-5", task_type="evaluation",
        )
        assert result.text == '{"overall": 8}'
        assert calls[0].get("stream") is True
        assert calls[0].get("timeout") == 120  # evaluation non-stream timeout kept

    @pytest.mark.asyncio
    async def test_non_matching_model_keeps_true_nonstream(self):
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _nonstream_client(calls, [_nonstream_resp("ok")])
        result = await provider.generate(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o", task_type="chapter", stream=False,
        )
        assert result.text == "ok"
        assert len(calls) == 1
        assert calls[0].get("stream") is False

    @pytest.mark.asyncio
    async def test_streaming_caller_unaffected(self):
        # stream=True callers keep the existing streamed-assembly behavior
        # (force_internal_stream only reroutes non-streaming callers).
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _stream_client(calls, [[_Chunk("ok")]])
        result = await provider.generate(
            [{"role": "user", "content": "hi"}],
            model="claude-sonnet-5", task_type="chapter", stream=True,
        )
        assert result.text == "ok"
        assert calls[0].get("stream") is True
        assert calls[0].get("timeout") is None  # streaming timeout untouched


class TestAttemptsFloor:
    def _captured_attempts(self, **gen_kw) -> int:
        provider = OpenAIProvider(api_key="k")
        captured: dict = {}

        async def fake_call_with_retry(fn, label="", attempts=4, **kwargs):
            captured["attempts"] = attempts
            return GenerationResult(
                text="ok", usage=TokenUsage(), model="m", provider="openai")

        with patch("app.services.llm_retry.call_with_retry",
                   side_effect=fake_call_with_retry):
            asyncio.run(provider.generate(
                [{"role": "user", "content": "hi"}], **gen_kw))
        return captured["attempts"]

    def test_forced_evaluation_floored_to_two(self):
        # evaluation defaults to attempts=1 (bounded non-stream timeouts);
        # internally-streamed evaluation needs >=2 so the retryable
        # empty-thinking-burn error can heal in place.
        assert self._captured_attempts(
            model="claude-sonnet-5", task_type="evaluation") == 2

    def test_forced_drafter_retry_attempts_one_floored_to_two(self):
        # drafter/rewrite pass explicit retry_attempts=1 from config; the
        # floor must win, otherwise the first burned attempt propagates.
        assert self._captured_attempts(
            model="claude-sonnet-5", task_type="chapter",
            stream=False, retry_attempts=1) == 2

    def test_forced_explicit_higher_retry_attempts_kept(self):
        assert self._captured_attempts(
            model="claude-sonnet-5", task_type="chapter",
            stream=False, retry_attempts=3) == 3

    def test_non_matching_defaults_unchanged(self):
        assert self._captured_attempts(
            model="gpt-4o", task_type="evaluation") == 1
        assert self._captured_attempts(
            model="gpt-4o", task_type="chapter", stream=False) == 1
        assert self._captured_attempts(
            model="gpt-4o", task_type="chapter", stream=True) == 4


class TestEmptyWithMaxTokensRetryable:
    def test_error_is_classified_retryable(self):
        assert _is_retryable(LLMEmptyCompletionError("burn")) is True

    @pytest.mark.asyncio
    async def test_forced_stream_empty_burn_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)  # skip retry backoff
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _stream_client(calls, [
            # attempt 1: no content, budget fully burned on hidden thinking
            [_usage_chunk(prompt=42, completion=100)],
            # attempt 2: healthy
            [_Chunk("正文"), _usage_chunk(prompt=42, completion=12)],
        ])
        result = await provider.generate(
            [{"role": "user", "content": "写"}],
            model="claude-sonnet-5", task_type="chapter",
            stream=False, max_tokens=100,
        )
        assert result.text == "正文"
        assert result.usage.output_tokens == 12
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_true_nonstream_empty_burn_raises_retryable(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _nonstream_client(calls, [
            _nonstream_resp("", prompt=42, completion=100),
        ])
        with pytest.raises(LLMEmptyCompletionError):
            await provider.generate(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o", task_type="chapter",
                stream=False, max_tokens=100,
            )
        # default attempts=1 for true non-stream: raised, not retried here —
        # tier fallback / task-level retry takes over.
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_true_nonstream_empty_burn_second_attempt_succeeds(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _nonstream_client(calls, [
            _nonstream_resp("", prompt=42, completion=100),
            _nonstream_resp("ok", prompt=42, completion=5),
        ])
        result = await provider.generate(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o", task_type="chapter",
            stream=False, max_tokens=100, retry_attempts=2,
        )
        assert result.text == "ok"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_empty_below_budget_keeps_old_success_with_empty(self):
        # Empty text WITHOUT the budget-exhausted signature is not the
        # thinking-burn pattern: cooldown only, empty result returned.
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _nonstream_client(calls, [
            _nonstream_resp("", prompt=42, completion=3),
        ])
        result = await provider.generate(
            [{"role": "user", "content": "hi"}],
            model="gpt-4o", task_type="chapter",
            stream=False, max_tokens=100,
        )
        assert result.text == ""
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cooldown_still_fires_before_raise(self, monkeypatch):
        cooled = {"n": 0}

        async def fake_cooldown(base_url, model, task_type):
            cooled["n"] += 1

        monkeypatch.setattr(mr, "_cooldown_openai_compat_empty", fake_cooldown)
        provider = OpenAIProvider(api_key="k")
        provider._client = _nonstream_client([], [
            _nonstream_resp("", prompt=42, completion=100),
        ])
        with pytest.raises(LLMEmptyCompletionError):
            await provider.generate(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o", task_type="chapter",
                stream=False, max_tokens=100,
            )
        assert cooled["n"] == 1


class TestSystemFoldStillApplies:
    @pytest.mark.asyncio
    async def test_forced_stream_path_still_merges_system(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "LLM_MERGE_SYSTEM_INTO_USER_MODELS", "claude-")
        provider = OpenAIProvider(api_key="k")
        calls: list[dict] = []
        provider._client = _stream_client(calls, [[_Chunk("ok")]])
        await provider.generate(
            [
                {"role": "system", "content": "主角叫虞千帆。"},
                {"role": "user", "content": "写第一场。"},
            ],
            model="claude-sonnet-5", task_type="chapter", stream=False,
        )
        sent = calls[0]["messages"]
        assert all(m["role"] != "system" for m in sent)
        assert "虞千帆" in sent[0]["content"]
