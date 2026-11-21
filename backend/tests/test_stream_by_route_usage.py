"""`stream_by_route` 与 Anthropic 流式也要记用量(2026-11-17)。

此前这两条路径的 `llm_call_logs` 恒为 `0/0`:用量页看不到走"显式路由"的调用
(弧规划、部分后台任务),也看不到 Anthropic 流式消耗 —— 而"看得见花了多少"正是
本批要吸收的能力。这里锁死:provider 报了用量就必须落到日志上下文里;
provider 不报时不能凭空编造(保持 0),也不能让流中断。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from app.services import model_router


class _Provider:
    def __init__(self, name: str, box_values: dict | None):
        self.name = name
        self.box_values = box_values

    async def generate_stream(self, **kwargs):
        box = kwargs.get("usage_box")
        if isinstance(box, dict) and self.box_values:
            box.update(self.box_values)
        yield "第一段。"
        yield "第二段。"


async def _run(monkeypatch, provider):
    from app.db import session
    from app.services import llm_call_logger

    events: list[tuple] = []

    @asynccontextmanager
    async def sessions():
        yield NS()

    @asynccontextmanager
    async def log(**kwargs):
        yield NS(
            add_chunk=lambda text: events.append(("chunk", text)),
            set_usage=lambda *args: events.append(("usage",) + args),
        )

    async def commit(*args, **kwargs):
        events.append(("committed",))

    monkeypatch.setattr(session, "async_session_factory", sessions)
    monkeypatch.setattr(llm_call_logger, "log_llm_call", log)
    monkeypatch.setattr(model_router, "_commit_llm_call_log", commit)

    router = model_router.ModelRouter()
    router.register_provider(provider.name, provider)
    route = NS(endpoint_id=provider.name, model="m", temperature=0.5, max_tokens=100)
    chunks = []
    async for chunk in router.stream_by_route(route, [], _log_meta={}):
        chunks.append(chunk)
    return chunks, events


@pytest.mark.asyncio
async def test_stream_by_route_records_provider_usage(monkeypatch):
    provider = _Provider("fake", {"input_tokens": 1200, "output_tokens": 340,
                                  "cached_input_tokens": 800, "usage_estimated": True})
    chunks, events = await _run(monkeypatch, provider)

    assert "".join(chunks) == "第一段。第二段。"
    usage = [e for e in events if e[0] == "usage"]
    assert usage == [("usage", 1200, 340, 800, True)]
    assert ("committed",) in events


@pytest.mark.asyncio
async def test_stream_by_route_without_provider_usage_keeps_zero_and_does_not_break(monkeypatch):
    provider = _Provider("fake", None)
    chunks, events = await _run(monkeypatch, provider)

    assert "".join(chunks) == "第一段。第二段。"
    # 没有用量就不回填(不编造),但日志照常收尾。
    assert not [e for e in events if e[0] == "usage"]
    assert ("committed",) in events


@pytest.mark.asyncio
async def test_stream_by_route_does_not_override_caller_supplied_box(monkeypatch):
    """调用方自己传了 usage_box 时不能被覆盖(否则会丢调用方的统计)。"""
    sentinel = {"input_tokens": 7}
    captured: dict = {}

    class Provider:
        name = "fake"

        async def generate_stream(self, **kwargs):
            captured.update(kwargs)
            yield "正文。"

    monkeypatch.setattr("app.services.model_router.ModelRouter._get_provider",
                        lambda self, key: Provider(), raising=True)
    from app.db import session
    from app.services import llm_call_logger

    @asynccontextmanager
    async def sessions():
        yield NS()

    @asynccontextmanager
    async def log(**kwargs):
        yield NS(add_chunk=lambda text: None, set_usage=lambda *a: None)

    async def commit(*a, **k):
        return None

    monkeypatch.setattr(session, "async_session_factory", sessions)
    monkeypatch.setattr(llm_call_logger, "log_llm_call", log)
    monkeypatch.setattr(model_router, "_commit_llm_call_log", commit)

    router = model_router.ModelRouter()
    route = NS(endpoint_id="fake", model="m", temperature=0.5, max_tokens=100)
    async for _ in router.stream_by_route(route, [], _log_meta={}, usage_box=sentinel):
        pass
    assert captured.get("usage_box") is sentinel

# --- Anthropic 流式:final.usage 必须进 usage_box -------------------------

class _FakeAnthropicStream:
    def __init__(self, chunks: list[str], usage):
        self._chunks = chunks
        self._usage = usage

    @property
    async def text_stream(self):
        for chunk in self._chunks:
            yield chunk

    async def get_final_message(self):
        return NS(stop_reason="end_turn", usage=self._usage)


class _FakeMessages:
    def __init__(self, stream):
        self._stream = stream

    def stream(self, **params):
        stream = self._stream

        @asynccontextmanager
        async def _ctx():
            yield stream

        return _ctx()


@pytest.mark.asyncio
async def test_anthropic_stream_fills_usage_box():
    """Anthropic 流式此前只写 Prometheus,日志恒为 0;现在必须落进 box。"""
    provider = model_router.AnthropicProvider(api_key="test")
    usage = NS(input_tokens=900, output_tokens=250,
               cache_read_input_tokens=600, cache_creation_input_tokens=100)
    stream = _FakeAnthropicStream(["正文"], usage)
    provider._client = NS(messages=_FakeMessages(stream))

    box: dict = {}
    chunks = [c async for c in provider.generate_stream(
        messages=[{"role": "user", "content": "写"}],
        model="claude-x", usage_box=box, task_type="scene_writer",
    )]

    assert "".join(chunks) == "正文"
    # 口径与非流式一致:input = 未缓存 + 缓存写入 + 缓存命中。
    assert box["input_tokens"] == 900 + 100 + 600
    assert box["output_tokens"] == 250
    assert box["cached_input_tokens"] == 600
    assert box["usage_estimated"] is False


@pytest.mark.asyncio
async def test_anthropic_stream_without_box_does_not_crash():
    provider = model_router.AnthropicProvider(api_key="test")
    usage = NS(input_tokens=10, output_tokens=2,
               cache_read_input_tokens=0, cache_creation_input_tokens=0)
    provider._client = NS(messages=_FakeMessages(_FakeAnthropicStream(["x"], usage)))
    chunks = [c async for c in provider.generate_stream(messages=[], model="claude-x")]
    assert chunks == ["x"]
