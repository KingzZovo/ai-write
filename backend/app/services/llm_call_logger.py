"""Async context manager that persists every LLM call into llm_call_logs."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_log import LLMCallLog

logger = logging.getLogger(__name__)


def _coerce_uuid_or_none(value: Any) -> Any:
    """Return value if it is a valid UUID (or UUID-shaped str), else None.

    ``endpoint_id`` is a UUID FK column. When model_router falls back to the
    env-based provider, the provider key is a plain string like ``env_openai``.
    Passing that straight to asyncpg raises DataError, which ABORTS the whole
    session transaction (not just the log insert) and closes the connection,
    cascading into unrelated rollback failures downstream. Coerce any
    non-UUID endpoint id to None (the column is nullable) so a fallback call
    logs cleanly instead of poisoning the request's DB session.
    """
    if value is None:
        return None
    import uuid as _uuid

    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None



@dataclass
class CallContext:
    """Accumulates chunks + token usage during a streaming call."""

    chunks: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # a1001960: prompt-cache hits (a subset of input_tokens). Filled by the
    # provider adapters via ``usage_box`` *during* the call, so this is only
    # known after the body ran — hence a context field rather than the
    # ``cached_input_tokens=`` parameter of :func:`log_llm_call`.
    cached_input_tokens: int = 0
    # a1001961: True ⇒ input_tokens/output_tokens below are the character-count
    # estimate, not provider metering. Set by model_router from ``usage_box``
    # once the provider has finished without reporting usage at all.
    usage_estimated: bool = False

    def add_chunk(self, text: str) -> None:
        self.chunks.append(text)

    def set_usage(self, input_tokens: int, output_tokens: int,
                  cached_input_tokens: int | None = None,
                  usage_estimated: bool | None = None) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        if cached_input_tokens is not None:
            self.cached_input_tokens = int(cached_input_tokens or 0)
        if usage_estimated is not None:
            self.usage_estimated = bool(usage_estimated)

    @property
    def response_text(self) -> str:
        return "".join(self.chunks)


def _estimate_cost_cents(prompt_tokens: int, completion_tokens: int) -> int:
    """按"每千 token 单价"估算成本,默认 0(不改变现有行为)。

    配额中间件比对的是 ``usage_quotas.cost_cents``;单价为 0 时无论用量多少都不会
    触发 402 —— 需要真正启用成本配额时设置 ``USAGE_CENTS_PER_1K_TOKENS``(分)。
    """
    import os

    raw = (os.getenv("USAGE_CENTS_PER_1K_TOKENS") or "").strip()
    if not raw:
        return 0
    try:
        unit = float(raw)
    except ValueError:
        logger.warning("USAGE_CENTS_PER_1K_TOKENS is not a number; ignored")
        return 0
    if unit <= 0:
        return 0
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    return int(round(total / 1000.0 * unit))
@asynccontextmanager
async def log_llm_call(
    *,
    db: AsyncSession,
    task_type: str,
    prompt_id: Any,
    project_id: Any,
    chapter_id: Any,
    messages: list[dict],
    rag_hits: list[dict] | None,
    model: str,
    endpoint_id: Any,
    tier_used: str | None = None,
    fallback_reason: str | None = None,
    attempt_index: int = 0,
    cached_input_tokens: int = 0,
    usage_estimated: bool = False,
):
    """Yield a `CallContext`. On exit, insert an LLMCallLog row.

    Errors and cancellations bubble up; the error row is flushed to the
    caller's transaction. Its owner must commit even when the call raises. DB write failures are warnings and never mask the
    original exception.

    v1.5.0 B1: ``tier_used``, ``fallback_reason``, ``attempt_index`` capture
    tier-aware fallback chain decisions. For non-fallback calls these stay
    at their defaults (None, None, 0).

    a1001960: ``cached_input_tokens`` persists prompt-cache hits. It is a
    *subset* of ``input_tokens``, never an extra bucket. Callers that learn
    the value only after the provider answered (most of them) fill
    ``ctx.cached_input_tokens`` / ``ctx.set_usage(..., cached_input_tokens=)``
    instead; the context value wins over the parameter, and the parameter's
    default 0 keeps every existing caller working unchanged.

    a1001961: ``usage_estimated`` flags a row whose token counts are a
    character-count *estimate* because the provider returned no usage at all
    (some relays ignore ``stream_options.include_usage``). It never marks a row
    the provider actually billed, and the estimate never replaces real numbers.
    The context field wins over the parameter, exactly like
    ``cached_input_tokens``.
    """
    ctx = CallContext()
    start = time.monotonic()
    status = "ok"
    error_message: str | None = None

    try:
        yield ctx
    except (Exception, asyncio.CancelledError, GeneratorExit) as e:
        status = "error"
        error_message = repr(e)
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            # Context (filled during the call) wins; the parameter is the
            # static fallback for callers that knew the hit count up front.
            _cached = int(ctx.cached_input_tokens or 0) or int(cached_input_tokens or 0)
            log = LLMCallLog(
                prompt_id=prompt_id,
                task_type=task_type,
                project_id=project_id,
                chapter_id=chapter_id,
                messages_json=messages,
                rag_hits_json=rag_hits,
                response_text=ctx.response_text,
                input_tokens=ctx.input_tokens,
                cached_input_tokens=max(_cached, 0),
                output_tokens=ctx.output_tokens,
                usage_estimated=bool(ctx.usage_estimated or usage_estimated),
                latency_ms=latency_ms,
                model=model,
                endpoint_id=_coerce_uuid_or_none(endpoint_id),
                status=status,
                error_message=error_message,
                tier_used=tier_used,
                fallback_reason=fallback_reason,
                attempt_index=attempt_index,
            )
            db.add(log)
            await db.flush()
            # PR-USAGE-SYNC: same-transaction incr of usage_quotas so the
            # admin / quota UI reflects real consumption immediately.
            try:
                from app.services.usage_service import record_usage
                # PR-USAGE-FIX-IMPORT: app.middlewares.quota does NOT export
                # resolve_user_id_from_context or _current_user_id_ctx (verified
                # via grep). The previous import raised ImportError on every
                # call and the entire PR-USAGE-SYNC block fell through to the
                # warning except branch, so usage_quotas stayed at 0. For the
                # current single-user dev workspace just default to "king".
                # 2026-11-17: 原先写死 "king",而配额按 JWT sub 查 —— 其它用户
                # 的 cost_cents 永不增长,配额永不触发。现在取请求身份,后台任务
                # 没有请求上下文时才回退到默认账户。
                from app.middlewares.request_identity import resolve_user_id

                _user_id = resolve_user_id()
                _pt = int(ctx.input_tokens or 0)
                _ct = int(ctx.output_tokens or 0)
                if _pt or _ct:
                    await record_usage(
                        db,
                        user_id=str(_user_id),
                        prompt_tokens=_pt,
                        completion_tokens=_ct,
                        cost_cents=_estimate_cost_cents(_pt, _ct),
                    )
            except Exception as _us_err:
                logger.warning("PR-USAGE-SYNC: incr usage_quotas failed: %s", _us_err)
        except Exception as log_err:
            logger.warning("Failed to persist LLMCallLog: %s", log_err)
