"""按任务类型强制非流式 `LLM_NONSTREAM_TASKS`(2026-11-17)。

为什么需要:`deepseek-v4-flash` 是"先思考后输出"的模型,思考阶段很长时本网关会
提前关闭 SSE 流,管线收到 `APIError: upstream stream closed before any chunk
carried finish_reason`(规划类步骤思考最重、最先中招);同一模型**非流式**调用完全
正常。这个开关让运维按任务类型改走非流式。

这里锁死三件事:
1. 默认(开关空)行为与改动前一致 —— 流式入口仍然流式,`generate` 不额外传
   `stream=False`(即 provider 保持"默认流式");
2. 开关命中时:流式入口只 yield 一次、provider 被以**非流式**方式调用,但
   `usage_box` / `set_usage` / `_log_meta` 落库(committed)语义不变;
3. 解析容错(空格、空项、大小写、`*`、未知任务名不报错)。

全程纯离线:所有 provider 都是假的,不发任何请求。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from app.config import settings
from app.services import model_router
from app.services.model_router import (
    NONSTREAM_TASKS_ALL,
    GenerationResult,
    TokenUsage,
    _nonstream_tasks,
    _should_force_nonstream,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def switch_off(monkeypatch):
    """开关关闭:环境变量与配置项都清空(默认状态)。"""
    monkeypatch.delenv("LLM_NONSTREAM_TASKS", raising=False)
    monkeypatch.setattr(settings, "LLM_NONSTREAM_TASKS", "", raising=False)


def _enable(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LLM_NONSTREAM_TASKS", value)


def _route(ep_key: str = "fake"):
    return NS(endpoint_id=ep_key, model="m", temperature=0.5, max_tokens=100)


class _FakeProvider:
    """分别记录流式 / 非流式调用,便于断言"到底走了哪条路"。"""

    name = "fake"

    def __init__(self, *, text="第一段。第二段。第三段。", usage=None,
                 box_fill=None, error: Exception | None = None):
        self.text = text
        self.usage = usage or TokenUsage()
        self.box_fill = box_fill
        self.error = error
        self.gen_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    async def generate(self, messages=None, model="m", temperature=0.7,
                       max_tokens=100, **kw) -> GenerationResult:
        self.gen_calls.append(
            {"model": model, "temperature": temperature, "max_tokens": max_tokens, **kw})
        box = kw.get("usage_box")
        if isinstance(box, dict) and self.box_fill:
            box.update(self.box_fill)
        if self.error is not None:
            raise self.error
        return GenerationResult(text=self.text, usage=self.usage, model=model,
                                provider=self.name, finish_reason="stop")

    async def generate_stream(self, messages=None, model="m", temperature=0.7,
                              max_tokens=100, **kw):
        self.stream_calls.append({"model": model, **kw})
        box = kw.get("usage_box")
        if isinstance(box, dict) and self.box_fill:
            box.update(self.box_fill)
        for chunk in ("第一段。", "第二段。", "第三段。"):
            yield chunk


def _router(provider) -> model_router.ModelRouter:
    router = model_router.ModelRouter()
    router.register_provider(provider.name, provider)
    return router


def _install_logging(monkeypatch, events: list) -> None:
    """把 LLMCallLog 的 DB 侧换成事件列表(与既有流式用例同一套替身)。"""
    from app.db import session
    from app.services import llm_call_logger

    @asynccontextmanager
    async def sessions():
        yield NS()

    @asynccontextmanager
    async def log(**kwargs):
        yield NS(
            add_chunk=lambda text: events.append(("chunk", text)),
            set_usage=lambda *args, **kw: events.append(("usage", args, kw)),
            response_text="",
        )

    async def commit(*args, **kwargs):
        events.append(("committed",))

    monkeypatch.setattr(session, "async_session_factory", sessions)
    monkeypatch.setattr(llm_call_logger, "log_llm_call", log)
    monkeypatch.setattr(model_router, "_commit_llm_call_log", commit)


async def _noop(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# 解析与判定
# ---------------------------------------------------------------------------


def test_off_by_default(switch_off):
    assert _nonstream_tasks() == frozenset()
    assert _should_force_nonstream("scene_planner") is False
    assert _should_force_nonstream(None) is False
    assert _should_force_nonstream("") is False


def test_env_parsing_tolerates_spaces_case_and_empty_items(monkeypatch, switch_off):
    _enable(monkeypatch, " Scene_Planner ,, arc_planner , ,")
    assert _nonstream_tasks() == frozenset({"scene_planner", "arc_planner"})
    assert _should_force_nonstream("SCENE_PLANNER") is True
    assert _should_force_nonstream("  arc_planner  ") is True
    assert _should_force_nonstream("scene_writer") is False


def test_unknown_task_name_never_matches(monkeypatch, switch_off):
    """写错/未知的任务名不报错,只是永不匹配(开关不会拖垮所有调用)。"""
    _enable(monkeypatch, "scene_planer,,")          # 拼错了一个字母
    assert _nonstream_tasks() == frozenset({"scene_planer"})
    assert _should_force_nonstream("scene_planer") is True
    assert _should_force_nonstream("scene_planner") is False
    assert _should_force_nonstream("definitely_not_a_task") is False
    assert _should_force_nonstream(None) is False


def test_wildcard_matches_every_task(monkeypatch, switch_off):
    _enable(monkeypatch, " * ")
    assert NONSTREAM_TASKS_ALL in _nonstream_tasks()
    for task in ("scene_planner", "evaluation", "some_future_task", "", None):
        assert _should_force_nonstream(task) is True


def test_env_wins_over_config_item(monkeypatch, switch_off):
    monkeypatch.setattr(settings, "LLM_NONSTREAM_TASKS", "evaluation,chapter_evaluation")
    assert _should_force_nonstream("evaluation") is True

    _enable(monkeypatch, "scene_planner")
    assert _should_force_nonstream("scene_planner") is True
    assert _should_force_nonstream("evaluation") is False

    # 环境变量给空/空白 = 未设置 ⇒ 回落配置项。
    _enable(monkeypatch, "   ")
    assert _should_force_nonstream("evaluation") is True


# ---------------------------------------------------------------------------
# 默认关闭:流式入口仍然流式,generate 不强制非流式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_by_route_default_keeps_streaming(monkeypatch, switch_off):
    provider = _FakeProvider()
    router = _router(provider)

    chunks = [c async for c in router.stream_by_route(
        _route(), [], task_type="scene_planner")]

    assert len(chunks) == 3  # 多个 chunk = 真的在流式
    assert len(provider.stream_calls) == 1
    assert provider.gen_calls == []


@pytest.mark.asyncio
async def test_stream_with_tier_fallback_default_keeps_streaming(monkeypatch, switch_off):
    provider = _FakeProvider()
    router = _router(provider)
    monkeypatch.setattr(router, "_build_tier_attempts",
                        lambda **kw: [("standard", "fake", "m")])

    chunks = [c async for c in router.stream_with_tier_fallback(
        "scene_planner", [], route=_route())]

    assert len(chunks) == 3
    assert len(provider.stream_calls) == 1
    assert provider.gen_calls == []


@pytest.mark.asyncio
async def test_router_generate_default_does_not_force_nonstream(monkeypatch, switch_off):
    provider = _FakeProvider()
    router = _router(provider)

    result = await router.generate("scene_planner", [])

    assert result.text == provider.text
    # 没有显式 stream=False ⇒ provider 保持它自己的默认(``stream=True`` = 流式)。
    assert "stream" not in provider.gen_calls[0]


@pytest.mark.asyncio
async def test_router_generate_by_route_default_does_not_force_nonstream(
        monkeypatch, switch_off):
    provider = _FakeProvider()
    router = _router(provider)

    await router.generate_by_route(_route(), [], task_type="scene_planner")

    assert "stream" not in provider.gen_calls[0]


# ---------------------------------------------------------------------------
# 命中:流式入口只 yield 一次,provider 被非流式调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_by_route_forced_yields_once(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    provider = _FakeProvider()
    router = _router(provider)

    chunks = [c async for c in router.stream_by_route(
        _route(), [], task_type="scene_planner")]

    assert chunks == [provider.text]          # 单个 chunk
    assert provider.stream_calls == []        # 没有开 SSE
    assert provider.gen_calls[0]["stream"] is False


@pytest.mark.asyncio
async def test_stream_with_tier_fallback_forced_yields_once(monkeypatch):
    _enable(monkeypatch, "*")
    provider = _FakeProvider()
    router = _router(provider)
    monkeypatch.setattr(router, "_build_tier_attempts",
                        lambda **kw: [("standard", "fake", "m")])

    chunks = [c async for c in router.stream_with_tier_fallback(
        "scene_planner", [], route=_route())]

    assert chunks == [provider.text]
    assert provider.stream_calls == []
    assert provider.gen_calls[0]["stream"] is False


@pytest.mark.asyncio
async def test_stream_by_route_forced_is_still_an_async_generator(monkeypatch):
    """调用方仍是 ``async for`` + ``aclose``:只读一个 chunk 也能正常收尾。"""
    _enable(monkeypatch, "scene_planner")
    provider = _FakeProvider()
    router = _router(provider)

    stream = router.stream_by_route(_route(), [], task_type="scene_planner")
    assert await anext(stream) == provider.text
    await stream.aclose()

    stream2 = router.stream_by_route(_route(), [], task_type="scene_planner")
    assert await anext(stream2) == provider.text
    with pytest.raises(StopAsyncIteration):
        await anext(stream2)


@pytest.mark.asyncio
async def test_router_generate_forced_passes_stream_false(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    provider = _FakeProvider()
    router = _router(provider)

    result = await router.generate("scene_planner", [])

    assert result.text == provider.text
    assert provider.gen_calls[0]["stream"] is False
    # 未命中的任务不受影响(逐字不变)。
    await router.generate("scene_writer", [])
    assert "stream" not in provider.gen_calls[1]


@pytest.mark.asyncio
async def test_generate_by_route_forced_passes_stream_false(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    provider = _FakeProvider()
    router = _router(provider)

    await router.generate_by_route(_route(), [], task_type="scene_planner")

    assert provider.gen_calls[0]["stream"] is False


@pytest.mark.asyncio
async def test_forced_nonstream_keeps_tier_fallback_before_first_yield(monkeypatch):
    """非流式替代实现仍在"未产出任何 chunk"时才回退(语义不变)。"""
    _enable(monkeypatch, "scene_planner")
    failing = _FakeProvider(error=RuntimeError("boom"))
    good = _FakeProvider()
    router = model_router.ModelRouter()
    router.register_provider("first", failing)
    router.register_provider("second", good)
    monkeypatch.setattr(
        router, "_build_tier_attempts",
        lambda **kw: [("standard", "first", "m"), ("small", "second", "m")])

    chunks = [c async for c in router.stream_with_tier_fallback(
        "scene_planner", [], route=_route())]

    assert chunks == [good.text]
    assert good.gen_calls[0]["stream"] is False


# ---------------------------------------------------------------------------
# 命中:用量与落库语义不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_by_route_forced_logs_usage_and_commits(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    events: list = []
    _install_logging(monkeypatch, events)
    provider = _FakeProvider(box_fill={
        "input_tokens": 1200, "output_tokens": 340,
        "cached_input_tokens": 800, "usage_estimated": True,
    })
    router = _router(provider)

    chunks = [c async for c in router.stream_by_route(
        _route(), [], _log_meta={}, task_type="scene_planner")]

    assert chunks == [provider.text]
    assert provider.stream_calls == []
    assert provider.gen_calls[0]["stream"] is False
    # 落库路径仍然走到:正文入库、用量参数来自 usage_box、已提交。
    assert events.count(("chunk", provider.text)) == 1
    assert [e for e in events if e[0] == "usage"] == [
        ("usage", (1200, 340, 800, True), {})]
    assert ("committed",) in events


@pytest.mark.asyncio
async def test_stream_by_route_forced_closed_stream_still_commits(monkeypatch):
    """只读一个 chunk 就 aclose:日志上下文与提交照常收尾(与流式路径一致)。"""
    _enable(monkeypatch, "scene_planner")
    events: list = []
    _install_logging(monkeypatch, events)
    provider = _FakeProvider(box_fill={"input_tokens": 9, "output_tokens": 4})
    router = _router(provider)

    stream = router.stream_by_route(_route(), [], _log_meta={}, task_type="scene_planner")
    assert await anext(stream) == provider.text
    await stream.aclose()

    assert ("committed",) in events
    assert [e for e in events if e[0] == "usage"] == [
        ("usage", (9, 4, 0, None), {})]


@pytest.mark.asyncio
async def test_stream_with_tier_fallback_forced_logs_usage_and_commits(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    events: list = []
    _install_logging(monkeypatch, events)
    provider = _FakeProvider(box_fill={"input_tokens": 700, "output_tokens": 120,
                                       "usage_estimated": True})
    router = _router(provider)
    monkeypatch.setattr(router, "_build_tier_attempts",
                        lambda kw=None, **rest: [("standard", "fake", "m")])

    chunks = [c async for c in router.stream_with_tier_fallback(
        "scene_planner", [], route=_route(), _log_meta={})]

    assert chunks == [provider.text]
    assert provider.stream_calls == []
    assert events.count(("chunk", provider.text)) == 1
    assert [e for e in events if e[0] == "usage"] == [
        ("usage", (700, 120, 0), {"usage_estimated": True})]
    assert ("committed",) in events


# ---------------------------------------------------------------------------
# OpenAIProvider:generate / generate_stream 两级
# ---------------------------------------------------------------------------


def _sdk_stream(chunks: list[str]):
    class _Stream:
        def __init__(self):
            self._pending = list(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._pending:
                raise StopAsyncIteration
            text = self._pending.pop(0)
            return NS(choices=[NS(delta=NS(content=text, reasoning_content=None),
                                  finish_reason="stop")],
                      usage=None)

        async def close(self):
            return None

    return _Stream()


def _sdk_completion(text="完整正文。", usage=NS(prompt_tokens=10, completion_tokens=5,
                                             total_tokens=15)):
    return NS(choices=[NS(message=NS(content=text), finish_reason="stop")], usage=usage)


def _fake_client(captured: list, response):
    async def create(**kwargs):
        captured.append(kwargs)
        return response

    return NS(chat=NS(completions=NS(create=create)))


def _openai_provider(monkeypatch, captured: list, response) -> model_router.OpenAIProvider:
    provider = model_router.OpenAIProvider(api_key="k")
    provider._client = _fake_client(captured, response)  # client 属性直接返回它
    monkeypatch.setattr(model_router, "_wait_openai_compat_turn", _noop)
    return provider


@pytest.mark.asyncio
async def test_openai_generate_default_still_streams_internally(monkeypatch, switch_off):
    """默认(关闭开关):``generate`` 仍按 model 的默认走内部流式。"""
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion())
    opened: list = []

    async def fake_create_stream(create_kw):
        opened.append(create_kw)
        return _sdk_stream(["正文"])

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create_stream, raising=False)

    result = await provider.generate(messages=[{"role": "user", "content": "写"}],
                                     model="gpt-x", task_type="scene_planner")

    assert result.text == "正文"
    assert len(opened) == 1, "默认仍是流式(内部开流),行为未变"
    assert captured == [], "默认没有落到非流式 create"


@pytest.mark.asyncio
async def test_openai_generate_forced_nonstream_even_when_caller_streams(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion())

    box: dict = {}
    result = await provider.generate(messages=[{"role": "user", "content": "写"}],
                                     model="gpt-x", task_type="scene_planner",
                                     stream=True, usage_box=box)

    assert result.text == "完整正文。"
    assert captured[0]["stream"] is False   # 调用方传了 True 也必须非流式
    assert box["input_tokens"] == 10 and box["output_tokens"] == 5
    assert "usage_estimated" not in box     # 供应商真报了数 ⇒ 不标估算


@pytest.mark.asyncio
async def test_openai_generate_forced_nonstream_default_task_unaffected(monkeypatch,
                                                                      switch_off):
    """关闭开关时 ``stream=True`` 的调用方语义不变(仍走内部流式)。"""
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion())
    opened: list = []

    async def fake_create_stream(create_kw):
        opened.append(create_kw)
        return _sdk_stream(["正文"])

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create_stream, raising=False)

    result = await provider.generate(messages=[], model="gpt-x",
                                     task_type="scene_planner", stream=True)

    assert result.text == "正文"
    assert len(opened) == 1, "关闭开关时 stream=True 仍走内部流式(语义不变)"
    assert captured == []


@pytest.mark.asyncio
async def test_openai_generate_stream_forced_yields_single_chunk(monkeypatch):
    _enable(monkeypatch, "scene_planner")
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion())
    stream_calls: list = []

    async def fake_create_stream(create_kw):
        stream_calls.append(create_kw)
        return _sdk_stream(["不应走到这里"])

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create_stream, raising=False)

    box: dict = {}
    chunks = [c async for c in provider.generate_stream(
        messages=[{"role": "user", "content": "写"}], model="gpt-x",
        task_type="scene_planner", usage_box=box, stream=True)]

    assert chunks == ["完整正文。"]          # 单个 chunk
    assert stream_calls == []               # 没有开 SSE(不走 _create_chat_stream)
    assert captured[0]["stream"] is False
    assert box["input_tokens"] == 10 and box["output_tokens"] == 5


@pytest.mark.asyncio
async def test_openai_generate_stream_forced_flags_estimated_usage(monkeypatch):
    """供应商不报用量时,usage_box 仍按既有口径标记"估算",不是 0。"""
    _enable(monkeypatch, "*")
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion(usage=None))

    box: dict = {}
    chunks = [c async for c in provider.generate_stream(
        messages=[{"role": "user", "content": "写" * 300}], model="gpt-x",
        task_type="whatever", usage_box=box)]

    assert chunks == ["完整正文。"]
    assert box["usage_estimated"] is True
    assert box["input_tokens"] > 0 and box["output_tokens"] > 0


@pytest.mark.asyncio
async def test_openai_generate_stream_default_still_streams_chunkwise(monkeypatch,
                                                                     switch_off):
    captured: list = []
    provider = _openai_provider(monkeypatch, captured, _sdk_completion())

    async def fake_create_stream(create_kw):
        return _sdk_stream(["一", "二", "三"])

    monkeypatch.setattr(provider, "_create_chat_stream", fake_create_stream, raising=False)

    chunks = [c async for c in provider.generate_stream(
        messages=[{"role": "user", "content": "写"}], model="gpt-x",
        task_type="scene_planner")]

    assert chunks == ["一", "二", "三"]     # 逐 chunk 流出 = 默认行为不变
    assert captured == []                   # 没有落到非流式 create


# --- 非流式超时(2026-11-17) --------------------------------------------
# 非流式默认 45s,对"先思考后输出"的模型(实测单章 370s)必然不够;
# 运维用 LLM_NONSTREAM_TIMEOUT_SECONDS 调大,不设置时行为不变。

def test_nonstream_timeout_default_is_45(monkeypatch):
    from app.services.model_router import _resolve_nonstream_timeout
    monkeypatch.delenv("LLM_NONSTREAM_TIMEOUT_SECONDS", raising=False)
    assert _resolve_nonstream_timeout(None, "scene_planner") == 45


def test_nonstream_timeout_env_override(monkeypatch):
    from app.services.model_router import _resolve_nonstream_timeout
    monkeypatch.setenv("LLM_NONSTREAM_TIMEOUT_SECONDS", "240")
    assert _resolve_nonstream_timeout(None, "scene_planner") == 240.0


def test_nonstream_timeout_explicit_wins(monkeypatch):
    from app.services.model_router import _resolve_nonstream_timeout
    monkeypatch.setenv("LLM_NONSTREAM_TIMEOUT_SECONDS", "240")
    assert _resolve_nonstream_timeout(30, "scene_planner") == 30


def test_nonstream_timeout_bad_value_falls_back(monkeypatch):
    from app.services.model_router import _resolve_nonstream_timeout
    monkeypatch.setenv("LLM_NONSTREAM_TIMEOUT_SECONDS", "abc")
    assert _resolve_nonstream_timeout(None, "scene_planner") == 45
    monkeypatch.setenv("LLM_NONSTREAM_TIMEOUT_SECONDS", "-5")
    assert _resolve_nonstream_timeout(None, "scene_planner") == 45


def test_evaluation_timeout_unaffected(monkeypatch):
    from app.services.model_router import _resolve_nonstream_timeout
    monkeypatch.delenv("LLM_NONSTREAM_TIMEOUT_SECONDS", raising=False)
    assert _resolve_nonstream_timeout(None, "evaluation") == 120
