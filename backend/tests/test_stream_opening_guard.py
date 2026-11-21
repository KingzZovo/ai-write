"""回归:流式开头守卫不能把推理模型的思考前缀当成"诊断文本"。

实测(2026-11-15,`deepseek-v4-flash` 经 `http://127.0.0.1:8317`):
长提示下模型先吐 87 个只有 `reasoning_content` 的 chunk,正文到第 89 个 chunk 才出现。
旧实现把所有 chunk 记进同一个 256 上限,于是真实连续链**第 1 章**就报
`LLMRelayResponseError: Relay stream opening exceeded validation buffer`,
换模型即不可用。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.llm_retry import LLMRelayResponseError
from app.services.provider_response_guard import guard_chat_stream

HEADER = "⚠️ Upstream Gemini returned an empty response. If this Worker runs on Cloudflare"


def _chunk(content=None, reasoning=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


async def _aiter(chunks):
    for chunk in chunks:
        yield chunk


async def _collect(chunks):
    out = []
    async for chunk in guard_chat_stream(_aiter(chunks)):
        delta = chunk.choices[0].delta
        if delta.content:
            out.append(delta.content)
    return out


def test_long_reasoning_prefix_does_not_trip_the_guard():
    chunks = [_chunk(reasoning="思考…") for _ in range(400)]
    chunks += [_chunk(content="周芸"), _chunk(content="把信封压在碗下。")]
    assert asyncio.run(_collect(chunks)) == ["周芸", "把信封压在碗下。"]


def test_relay_diagnostic_is_still_rejected():
    chunks = [_chunk(content=HEADER), _chunk(content=" more")]
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect(chunks))


def test_bounded_prefix_still_raises_when_only_whitespace_arrives():
    """全是空白等于没有正文:4096 字前缀上界仍然要挡住,不能无限攒。"""
    chunks = [_chunk(content="   ") for _ in range(1400)]
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect(chunks))


def test_total_chunk_bound_still_raises():
    """思考前缀本身也要有界,不能靠它绕开缓冲上限。"""
    chunks = [_chunk(reasoning="思考…") for _ in range(8200)]
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect(chunks))


# 2026-11-16 实测:上游把"预算全烧在思考上"翻成自然语言、整段当正文返回;
# 网关自身还有 terminal_empty_incomplete。两者都必须在流开头就被拒。
BUDGET_DIAGNOSTIC = (
    "The model used its entire output budget on reasoning and returned no "
    "answer. Raise max_tokens or simplify the request."
)


@pytest.mark.parametrize("opening", [
    BUDGET_DIAGNOSTIC,
    "terminal_empty_incomplete",
    "stream error: upstream terminated with incomplete empty response (0 tokens)",
])
def test_new_relay_diagnostics_are_rejected_at_stream_opening(opening):
    chunks = [_chunk(content=opening), _chunk(content="更多诊断文本")]
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect(chunks))


def test_budget_diagnostic_split_across_chunks_is_rejected():
    chunks = [_chunk(content="The model used its entire output"),
              _chunk(content=" budget on reasoning and returned no answer.")]
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect(chunks))


@pytest.mark.parametrize("opening", [
    "[错误] 账号未验证,需要人工完成验证: 创建会话 HTTP 403",
    "account_needs_verification",
])
def test_account_verification_diagnostics_are_rejected(opening):
    """账号侧拒绝也是"开头即诊断":必须在流开头就被拒,不能落到正文里。"""
    with pytest.raises(LLMRelayResponseError):
        asyncio.run(_collect([_chunk(content=opening), _chunk(content="更多诊断文本")]))
