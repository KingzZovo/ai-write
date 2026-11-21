"""平台级额外请求体 `LLM_EXTRA_BODY_JSON`(2026-11-17)。

为什么需要它:`deepseek-v4-flash` 是"先思考后输出"的模型,长 prompt 下首段正文要等
26.0s(期间只有 reasoning_content),而本网关的流式请求约 60s 就被关闭 —— 管线在
规划/正文步骤直接失败(`upstream stream closed before any chunk carried finish_reason`)。
关掉思考后首正文 2.5s、总时长 10.9s,链路正常。这个开关让运维不必改代码就能给
这类模型加网关参数,同时保证:默认空、拼错不生效、不影响任何其他模型。
"""

from __future__ import annotations

import json

import pytest

from app.services import model_router
from app.services.model_router import _env_extra_body


def test_parses_json_object(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY_JSON", '{"thinking":{"type":"disabled"}}')
    assert _env_extra_body() == {"thinking": {"type": "disabled"}}


def test_empty_env_means_no_change(monkeypatch):
    monkeypatch.delenv("LLM_EXTRA_BODY_JSON", raising=False)
    assert _env_extra_body() == {}
    monkeypatch.setenv("LLM_EXTRA_BODY_JSON", "   ")
    assert _env_extra_body() == {}


@pytest.mark.parametrize("raw", ["not json", "{", '{"a":}', "[1,2,3]", '"text"', "42"])
def test_invalid_or_non_object_is_ignored(monkeypatch, raw):
    monkeypatch.setenv("LLM_EXTRA_BODY_JSON", raw)
    assert _env_extra_body() == {}


class _FakeStream:
    """最小可用的 chat stream:一个带 finish_reason 的终态 chunk。"""

    def __init__(self):
        self._chunks = [object(), object()]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        self._chunks.pop()
        import types
        choice = types.SimpleNamespace(delta=types.SimpleNamespace(content="正文", reasoning_content=None),
                                       finish_reason="stop")
        return types.SimpleNamespace(choices=[choice], usage=None)


@pytest.mark.asyncio
async def test_stream_call_carries_env_extra_body(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY_JSON", '{"thinking":{"type":"disabled"}}')
    provider = model_router.OpenAIProvider(api_key="k")
    captured: dict = {}

    async def fake_create(create_kw, *args, **kwargs):
        captured.update(create_kw)
        return _FakeStream()

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create, raising=False)
    monkeypatch.setattr(model_router, "_wait_openai_compat_turn", _noop, raising=False)

    chunks = [chunk async for chunk in provider.generate_stream(
        messages=[{"role": "user", "content": "写"}], model="deepseek-v4-flash",
        task_type="scene_writer")]
    assert "".join(chunks) == "正文正文"
    assert captured["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_no_env_means_extra_body_stays_absent(monkeypatch):
    monkeypatch.delenv("LLM_EXTRA_BODY_JSON", raising=False)
    provider = model_router.OpenAIProvider(api_key="k")
    captured: dict = {}

    async def fake_create(create_kw, *args, **kwargs):
        captured.update(create_kw)
        return _FakeStream()

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create, raising=False)
    monkeypatch.setattr(model_router, "_wait_openai_compat_turn", _noop, raising=False)
    _ = [chunk async for chunk in provider.generate_stream(
        messages=[{"role": "user", "content": "写"}], model="gpt-4o", task_type="scene_writer")]
    # 没有开关就没有额外的键(只有原本可能存在的 prompt_cache_key)。
    assert "thinking" not in (captured.get("extra_body") or {})


async def _noop(*args, **kwargs):
    return None