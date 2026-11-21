"""Recognize observed HTTP-200 relay failures without judging narrative content."""
from app.services.llm_retry import LLMRelayResponseError

# Match only an answer-opening transport diagnostic, never dialogue/substrings.
_HEADERS = (
    "⚠️ Upstream Gemini returned an empty response.",
    "⚠ Upstream Gemini returned an empty response.",
    "Upstream Gemini returned an empty response.",
)

# 2026-11-16 实测:中转还会把"自己的错误码"和"上游对预算耗尽的自然语言说明"
# 当 HTTP-200 正文送出(网关二进制里的 terminal_empty_incomplete /
# stream error: upstream terminated with incomplete empty response (0 tokens);
# 上游透传的 "The model used its entire output budget on reasoning ...")。
# 与 Gemini 空响应同源:整段正文就是一句诊断。仍只在开头判定。
_DIAGNOSTIC_PREFIXES = (
    "terminal_empty_incomplete",
    "stream error: upstream terminated with incomplete empty response",
    "The model used its entire output budget on reasoning",
    # 2026-11-17 实测(中转对 `claude-sonnet-4-6` 通道):账号侧拒绝时，
    # HTTP 200 正文就是这一句 `[错误] 账号未验证,需要人工完成验证: ... HTTP 403 ...`。
    "account_needs_verification",
    "[错误]",
)
_OPENING_PREFIXES = _HEADERS + _DIAGNOSTIC_PREFIXES

# "这可能是诊断文本"的上界:带 content 的 chunk 数、以及被暂存的总 chunk 数
# (推理模型的思考前缀不计入前者,但整体仍需有界)。
_MAX_CONTENT_CHUNKS = 256
_MAX_TOTAL_CHUNKS = 8192
_MAX_PREFIX_CHARS = 4096


def reject_relay_error(text: str) -> None:
    if text.lstrip().startswith(_OPENING_PREFIXES):
        # Do not echo the response: relay diagnostics may contain credentials.
        raise LLMRelayResponseError("Relay returned an upstream/gateway failure as HTTP-200 content")


async def guard_chat_stream(stream):
    """Hold only the opening diagnostic prefix before exposing any SDK chunks.

    Usage/role chunks are held too, so retry's first-item boundary is not crossed
    by metadata preceding a service error. Ordinary prose is released as soon as
    it diverges from the observed headers. Never rewind content already yielded.

    **只有带 ``content`` 的 chunk 才算"这可能是诊断文本"**:推理模型的
    ``reasoning_content`` 前缀可以很长(实测 deepseek-v4-flash:长提示下正文
    在第 89 个 chunk 才出现,前面 87 个只有思考内容)。旧实现把所有 chunk 都记进
    同一个 256 上限,于是换一个推理模型就从第 1 章起
    ``Relay stream opening exceeded validation buffer``。
    """
    pending = []
    prefix = ""
    content_chunks = 0
    total_chunks = 0
    decided = False
    try:
        async for chunk in stream:
            if decided:
                yield chunk
                continue
            pending.append(chunk)
            total_chunks += 1
            # 部分 OpenAI 兼容中转折在拒绝/结束帧里发 delta=null,直接取
            # ``.delta.content`` 会 AttributeError;此时已 yield 过正文,
            # stream_with_retry 按设计不再重试 → 整章以异常收场。
            _delta = getattr(chunk.choices[0], "delta", None) if chunk.choices else None
            _content = getattr(_delta, "content", None)
            if _content:
                prefix += _content
                content_chunks += 1
            head = prefix.lstrip()
            reject_relay_error(head)
            if head and not any(header.startswith(head) for header in _OPENING_PREFIXES):
                decided = True
                for saved in pending:
                    yield saved
                pending.clear()
            elif (content_chunks > _MAX_CONTENT_CHUNKS
                  or total_chunks > _MAX_TOTAL_CHUNKS
                  or len(prefix) > _MAX_PREFIX_CHARS):
                # Bound pathological whitespace/metadata before meaningful text.
                raise LLMRelayResponseError("Relay stream opening exceeded validation buffer")
        for saved in pending:
            yield saved
    finally:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is not None:
            await close()


def require_normal_finish(reason: str | None) -> None:
    """Reject explicit truncation/filter/tool endings; legacy absence is unknown."""
    if reason is not None and reason not in {'stop', 'end_turn', 'stop_sequence'}:
        raise ValueError('provider_incomplete_response')


def require_nonempty_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError('provider_empty_response')
    return text


async def require_nonempty_stream(stream):
    """Preserve delivered chunks but reject normal EOF with no actual prose."""
    from contextlib import aclosing
    has_text = False
    async with aclosing(stream):
        async for chunk in stream:
            has_text = has_text or bool(chunk and chunk.strip())
            yield chunk
    if not has_text:
        raise ValueError('provider_empty_response')
