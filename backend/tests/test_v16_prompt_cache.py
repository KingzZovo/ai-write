"""v1.6.0 Y1+Y2 regression: prompt cache plumbing for AnthropicProvider/OpenAIProvider.

These tests do NOT call real LLM endpoints. They monkey-patch the provider's
`client` property to a fake object that captures the kwargs passed to
`messages.create` / `chat.completions.create`, so we can assert that:

- AnthropicProvider rewrites a long `system` prompt into a list of blocks
  carrying `cache_control: ephemeral` (gated by ANTHROPIC_CACHE_MIN_CHARS).
- AnthropicProvider passes a short system prompt through unchanged.
- OpenAIProvider injects `extra_body['prompt_cache_key'] = f'{task_type}:{model}'`.
- OpenAIProvider records cached_tokens to LLM_CACHE_TOKEN_TOTAL when usage
  reports `prompt_tokens_details.cached_tokens > 0`.
"""

import os
import pytest
from types import SimpleNamespace

from app.services.model_router import AnthropicProvider, OpenAIProvider


class _FakeAnthMessages:
    def __init__(self, captured: dict):
        self.captured = captured

    async def create(self, **params):
        self.captured.update(params)
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
        )
        content = [SimpleNamespace(text="hello")]
        return SimpleNamespace(content=content, usage=usage)


class _FakeAnthClient:
    def __init__(self, captured: dict):
        self.messages = _FakeAnthMessages(captured)


class _FakeUsage(SimpleNamespace):
    pass


class _FakeOAIChunk:
    def __init__(self, content=None, usage=None):
        self.choices = ([SimpleNamespace(delta=SimpleNamespace(content=content))]
                        if content is not None else [])
        self.usage = usage


class _FakeOAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _FakeOAICompletions:
    def __init__(self, captured: dict, chunks):
        self.captured = captured
        self.chunks = chunks

    async def create(self, **params):
        self.captured.update(params)
        return _FakeOAIStream(self.chunks)


class _FakeOAIChat:
    def __init__(self, captured: dict, chunks):
        self.completions = _FakeOAICompletions(captured, chunks)


class _FakeOAIClient:
    def __init__(self, captured: dict, chunks):
        self.chat = _FakeOAIChat(captured, chunks)


@pytest.mark.asyncio
async def test_anthropic_long_system_emits_cache_control():
    """system >= ANTHROPIC_CACHE_MIN_CHARS -> system becomes list with cache_control."""
    captured: dict = {}
    p = AnthropicProvider(api_key="sk-test")
    p._client = _FakeAnthClient(captured)
    long_sys = "x" * 5000  # > default 4096
    await p.generate(
        messages=[{"role": "system", "content": long_sys}, {"role": "user", "content": "hi"}],
        model="claude-sonnet-4-20250514",
        task_type="scene_writer",
    )
    assert isinstance(captured["system"], list), "system should be wrapped as block list"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["system"][0]["text"] == long_sys


@pytest.mark.asyncio
async def test_anthropic_short_system_passthrough():
    """system < ANTHROPIC_CACHE_MIN_CHARS -> system stays as plain string."""
    captured: dict = {}
    p = AnthropicProvider(api_key="sk-test")
    p._client = _FakeAnthClient(captured)
    short_sys = "short prompt"
    await p.generate(
        messages=[{"role": "system", "content": short_sys}, {"role": "user", "content": "hi"}],
        model="claude-sonnet-4-20250514",
        task_type="misc",
    )
    assert captured["system"] == short_sys, "short system should NOT be wrapped"


@pytest.mark.asyncio
async def test_openai_prompt_cache_key_injected():
    """OpenAI provider injects extra_body['prompt_cache_key'] = '{task}:{model}'."""
    captured: dict = {}
    chunks = [
        _FakeOAIChunk(content="hello"),
        _FakeOAIChunk(usage=_FakeUsage(
            prompt_tokens=50, completion_tokens=10, total_tokens=60,
            prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        )),
    ]
    p = OpenAIProvider(api_key="sk-test")
    p._client = _FakeOAIClient(captured, chunks)
    result = await p.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        task_type="outline_book",
    )
    assert captured["extra_body"] == {"prompt_cache_key": "outline_book:gpt-4o"}
    assert result.text == "hello"
    assert result.usage.input_tokens == 50


@pytest.mark.asyncio
async def test_openai_records_cached_tokens_metric():
    """When usage reports cached_tokens > 0, LLM_CACHE_TOKEN_TOTAL.cache_read increments."""
    from app.observability.metrics import LLM_CACHE_TOKEN_TOTAL

    def _read_count(task_type, provider, model, kind):
        try:
            sample = LLM_CACHE_TOKEN_TOTAL.labels(
                task_type=task_type, provider=provider, model=model, kind=kind
            )._value.get()
            return float(sample)
        except Exception:
            return 0.0

    before = _read_count("scene_writer", "openai", "gpt-4o-test", "cache_read")
    captured: dict = {}
    chunks = [
        _FakeOAIChunk(content="world"),
        _FakeOAIChunk(usage=_FakeUsage(
            prompt_tokens=200, completion_tokens=20, total_tokens=220,
            prompt_tokens_details=SimpleNamespace(cached_tokens=120),
        )),
    ]
    p = OpenAIProvider(api_key="sk-test")
    p._client = _FakeOAIClient(captured, chunks)
    await p.generate(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o-test",
        task_type="scene_writer",
    )
    after = _read_count("scene_writer", "openai", "gpt-4o-test", "cache_read")
    assert after - before >= 120, f"cache_read should grow by >=120, got {after - before}"
