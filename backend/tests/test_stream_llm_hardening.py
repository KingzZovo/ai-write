"""Streaming LLM call-path hardening.

Covers three audit findings:

1. ``OpenAIProvider.generate_stream`` had no retry at all — failures before
   the first chunk (connect errors, immediate 4xx/5xx) now retry in place via
   ``llm_retry.stream_with_retry``; after the first chunk errors still
   propagate immediately (the consumer's buffer cannot be rewound).
2. HTTP 400 (``openai.BadRequestError``) was terminally non-retryable, but
   relay fronts return transient 400s. 400s now participate in the normal
   attempt loop with a bounded budget (``_BAD_REQUEST_MAX_RETRIES``).
3. Streaming calls emitted no metrics/usage. ``stream_options.include_usage``
   is probed (disabled per-process for endpoints that reject it by name), the
   provider fills a ``usage_box``, and ``stream_with_tier_fallback`` wraps the
   stream in ``time_llm_call`` + ``ctx.set_usage``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

import app.services.model_router as mr
from app.observability.metrics import REGISTRY
from app.services.llm_retry import (
    _BAD_REQUEST_MAX_RETRIES,
    call_with_retry,
    stream_with_retry,
)
from app.services.model_router import BaseProvider, ModelRouter, OpenAIProvider


def _bad_request(msg: str) -> openai.BadRequestError:
    req = httpx.Request("POST", "http://relay.test/v1/chat/completions")
    resp = httpx.Response(400, request=req)
    return openai.BadRequestError(msg, response=resp, body=None)


@pytest.fixture(autouse=True)
def _clean_stream_options_cache():
    mr._STREAM_OPTIONS_UNSUPPORTED.clear()
    yield
    mr._STREAM_OPTIONS_UNSUPPORTED.clear()


# =========================================================================
# stream_with_retry — pre-first-chunk retry semantics
# =========================================================================

def _opener(fail_times: int, items: list[str], exc_factory=ConnectionError):
    """Zero-arg async open_stream that fails ``fail_times`` times, then streams."""
    state = {"opens": 0}

    async def _open():
        state["opens"] += 1
        if state["opens"] <= fail_times:
            raise exc_factory("boom") if exc_factory is ConnectionError else exc_factory()

        async def _gen():
            for it in items:
                yield it
        return _gen()

    return _open, state


@pytest.mark.asyncio
async def test_stream_with_retry_recovers_before_first_chunk():
    _open, state = _opener(fail_times=2, items=["a", "b"])
    out = [c async for c in stream_with_retry(
        _open, label="t", attempts=4, base_delay=0.0)]
    assert out == ["a", "b"]
    assert state["opens"] == 3


@pytest.mark.asyncio
async def test_stream_with_retry_no_retry_after_first_chunk():
    state = {"opens": 0}

    async def _open():
        state["opens"] += 1

        async def _gen():
            yield "a"
            raise ConnectionError("mid-stream")
        return _gen()

    out: list[str] = []
    with pytest.raises(ConnectionError):
        async for c in stream_with_retry(_open, label="t", attempts=4, base_delay=0.0):
            out.append(c)
    assert out == ["a"]
    assert state["opens"] == 1  # no re-open after a chunk was yielded


@pytest.mark.asyncio
async def test_stream_with_retry_nonretryable_raises_immediately():
    state = {"opens": 0}

    async def _open():
        state["opens"] += 1
        raise ValueError("genuine bug")

    with pytest.raises(ValueError):
        async for _ in stream_with_retry(_open, label="t", attempts=4, base_delay=0.0):
            pass
    assert state["opens"] == 1


@pytest.mark.asyncio
async def test_stream_with_retry_empty_stream_is_not_a_failure():
    _open, state = _opener(fail_times=0, items=[])
    out = [c async for c in stream_with_retry(
        _open, label="t", attempts=4, base_delay=0.0)]
    assert out == []
    assert state["opens"] == 1


@pytest.mark.asyncio
async def test_stream_with_retry_exhausts_attempts_then_raises():
    _open, state = _opener(fail_times=99, items=["a"])
    with pytest.raises(ConnectionError):
        async for _ in stream_with_retry(_open, label="t", attempts=3, base_delay=0.0):
            pass
    assert state["opens"] == 3


# =========================================================================
# Bounded 400 retry
# =========================================================================

@pytest.mark.asyncio
async def test_call_with_retry_400_is_bounded():
    calls = {"n": 0}

    async def _fn():
        calls["n"] += 1
        raise _bad_request("upstream account pool exhausted")

    with pytest.raises(openai.BadRequestError):
        await call_with_retry(_fn, label="t", attempts=10, base_delay=0.0)
    # 1 initial call + at most _BAD_REQUEST_MAX_RETRIES retries, even though
    # 10 attempts were allowed.
    assert calls["n"] == 1 + _BAD_REQUEST_MAX_RETRIES


@pytest.mark.asyncio
async def test_call_with_retry_transient_400_heals():
    calls = {"n": 0}

    async def _fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _bad_request("transient relay 400")
        return "ok"

    assert await call_with_retry(_fn, label="t", attempts=4, base_delay=0.0) == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_stream_with_retry_400_is_bounded():
    _open, state = _opener(
        fail_times=99, items=["a"],
        exc_factory=lambda: _bad_request("relay 400"))
    with pytest.raises(openai.BadRequestError):
        async for _ in stream_with_retry(_open, label="t", attempts=10, base_delay=0.0):
            pass
    assert state["opens"] == 1 + _BAD_REQUEST_MAX_RETRIES


# =========================================================================
# OpenAIProvider.generate_stream — retry, usage recording, include_usage probe
# =========================================================================

class _Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = ([SimpleNamespace(delta=SimpleNamespace(content=content))]
                        if content is not None else [])
        self.usage = usage


def _usage_chunk(prompt=11, completion=7):
    return _Chunk(usage=SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion, prompt_tokens_details=None))


def _fake_client(create):
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


async def _fast_sleep(_delay):
    return None


@pytest.mark.asyncio
async def test_generate_stream_retries_pre_first_chunk(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)  # skip real backoff
    provider = OpenAIProvider(api_key="k")
    calls = {"n": 0}

    async def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connect refused")

        async def _gen():
            yield _Chunk("he")
            yield _Chunk("llo")
        return _gen()

    provider._client = _fake_client(create)
    out = []
    async for c in provider.generate_stream(
            [{"role": "user", "content": "hi"}], model="m", task_type="scene_writer"):
        out.append(c)
    assert "".join(out) == "hello"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_generate_stream_no_retry_after_first_chunk(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    provider = OpenAIProvider(api_key="k")
    calls = {"n": 0}

    async def create(**kwargs):
        calls["n"] += 1

        async def _gen():
            yield _Chunk("partial")
            raise ConnectionError("mid-stream drop")
        return _gen()

    provider._client = _fake_client(create)
    out = []
    with pytest.raises(ConnectionError):
        async for c in provider.generate_stream(
                [{"role": "user", "content": "hi"}], model="m", task_type="scene_writer"):
            out.append(c)
    assert out == ["partial"]
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_generate_stream_records_usage_box():
    provider = OpenAIProvider(api_key="k")

    async def create(**kwargs):
        assert kwargs.get("stream_options") == {"include_usage": True}

        async def _gen():
            yield _Chunk("o")
            yield _Chunk("k")
            yield _usage_chunk(prompt=11, completion=7)
        return _gen()

    provider._client = _fake_client(create)
    box: dict = {}
    out = []
    async for c in provider.generate_stream(
            [{"role": "user", "content": "hi"}], model="m",
            task_type="scene_writer", usage_box=box):
        out.append(c)
    assert "".join(out) == "ok"
    assert box == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.asyncio
async def test_generate_stream_include_usage_probe_disables_on_named_400(monkeypatch):
    monkeypatch.setattr(mr, "_OPENAI_COMPAT_RATE_LIMIT_ENABLED", False)
    base_url = "http://relay.test/v1"
    provider = OpenAIProvider(api_key="k", base_url=base_url)
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if "stream_options" in kwargs:
            raise _bad_request("unknown field: stream_options")

        async def _gen():
            yield _Chunk("ok")
        return _gen()

    provider._client = _fake_client(create)

    async def _drain():
        return [c async for c in provider.generate_stream(
            [{"role": "user", "content": "hi"}], model="m", task_type="scene_writer")]

    assert await _drain() == ["ok"]
    # probe with the field, then immediate retry without it
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert base_url in mr._STREAM_OPTIONS_UNSUPPORTED
    # subsequent calls skip the probe entirely
    assert await _drain() == ["ok"]
    assert len(calls) == 3
    assert "stream_options" not in calls[2]


@pytest.mark.asyncio
async def test_generate_stream_opaque_400_retries_without_field_but_keeps_probe(monkeypatch):
    # A 400 that does NOT name the field: still retried once without the
    # field (no regression vs pre-probe behavior), but the endpoint is not
    # permanently blacklisted.
    monkeypatch.setattr(mr, "_OPENAI_COMPAT_RATE_LIMIT_ENABLED", False)
    provider = OpenAIProvider(api_key="k", base_url="http://relay.test/v1")
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if "stream_options" in kwargs:
            raise _bad_request("invalid request")

        async def _gen():
            yield _Chunk("ok")
        return _gen()

    provider._client = _fake_client(create)
    out = [c async for c in provider.generate_stream(
        [{"role": "user", "content": "hi"}], model="m", task_type="scene_writer")]
    assert out == ["ok"]
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert "http://relay.test/v1" not in mr._STREAM_OPTIONS_UNSUPPORTED


# =========================================================================
# stream_with_tier_fallback — metrics + usage wiring
# =========================================================================

class _UsageStreamProvider(BaseProvider):
    """Stub that streams two chunks and reports usage via usage_box."""

    name = "usage_stub"

    def __init__(self, *, fail_mid_stream: bool = False):
        self.calls = 0
        self.fail_mid_stream = fail_mid_stream

    async def generate(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        raise NotImplementedError

    async def generate_stream(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.calls += 1
        yield "he"
        if self.fail_mid_stream:
            raise RuntimeError("mid-stream boom")
        yield "llo"
        box = kw.get("usage_box")
        if box is not None:
            box["input_tokens"] = 23
            box["output_tokens"] = 5


def _make_router(primary: BaseProvider, secondary: BaseProvider | None = None) -> ModelRouter:
    r = ModelRouter.__new__(ModelRouter)
    r.providers = {"ep_a": primary}
    r._endpoint_tiers = {"ep_a": "standard"}
    r._endpoint_defaults = {"ep_a": "m-a"}
    if secondary is not None:
        r.providers["ep_b"] = secondary
        r._endpoint_tiers["ep_b"] = "small"
        r._endpoint_defaults["ep_b"] = "m-b"
    r._track = lambda usage: None
    return r


def _sample(name: str, labels: dict[str, str]) -> float:
    val = REGISTRY.get_sample_value(name, labels)
    return float(val) if val is not None else 0.0


@pytest.mark.asyncio
async def test_stream_with_tier_fallback_emits_metrics_and_tokens():
    prov = _UsageStreamProvider()
    r = _make_router(prov)
    labels = {"task_type": "scene_writer", "provider": "_UsageStreamProvider",
              "model": "m-a"}
    before_total = _sample("llm_call_total", {**labels, "status": "ok"})
    before_in = _sample("llm_token_total", {**labels, "direction": "input"})
    before_out = _sample("llm_token_total", {**labels, "direction": "output"})

    chunks = []
    async for c in r.stream_with_tier_fallback(
            "scene_writer", [{"role": "user", "content": "hi"}],
            preferred_tier="standard"):
        chunks.append(c)
    assert "".join(chunks) == "hello"

    assert _sample("llm_call_total", {**labels, "status": "ok"}) - before_total == 1.0
    assert _sample("llm_token_total", {**labels, "direction": "input"}) - before_in == 23.0
    assert _sample("llm_token_total", {**labels, "direction": "output"}) - before_out == 5.0


@pytest.mark.asyncio
async def test_stream_with_tier_fallback_mid_stream_error_no_fallback():
    prov = _UsageStreamProvider(fail_mid_stream=True)
    backup = _UsageStreamProvider()
    r = _make_router(prov, secondary=backup)
    chunks = []
    with pytest.raises(RuntimeError, match="mid-stream boom"):
        async for c in r.stream_with_tier_fallback(
                "scene_writer", [{"role": "user", "content": "hi"}],
                preferred_tier="standard"):
            chunks.append(c)
    assert chunks == ["he"]
    assert backup.calls == 0  # no tier fallback after a chunk was yielded
