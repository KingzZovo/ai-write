"""Async context manager that persists every LLM call into llm_call_logs."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_log import LLMCallLog

logger = logging.getLogger(__name__)


@dataclass
class CallContext:
    """Accumulates chunks + token usage during a streaming call."""

    chunks: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def add_chunk(self, text: str) -> None:
        self.chunks.append(text)

    def set_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def response_text(self) -> str:
        return "".join(self.chunks)


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
):
    """Yield a `CallContext`. On exit, insert an LLMCallLog row.

    Errors in the wrapped code bubble up, but the log row is still persisted
    with status='error'. DB write failures are warnings and never mask the
    original exception.
    """
    ctx = CallContext()
    start = time.monotonic()
    status = "ok"
    error_message: str | None = None

    try:
        yield ctx
    except Exception as e:
        status = "error"
        error_message = repr(e)
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            log = LLMCallLog(
                prompt_id=prompt_id,
                task_type=task_type,
                project_id=project_id,
                chapter_id=chapter_id,
                messages_json=messages,
                rag_hits_json=rag_hits,
                response_text=ctx.response_text,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                latency_ms=latency_ms,
                model=model,
                endpoint_id=endpoint_id,
                status=status,
                error_message=error_message,
            )
            db.add(log)
            await db.flush()
        except Exception as log_err:
            logger.warning("Failed to persist LLMCallLog: %s", log_err)
