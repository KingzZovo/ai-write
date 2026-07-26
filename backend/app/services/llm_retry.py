"""Shared async retry helper for LLM/embedding HTTP calls.

v1.14: Both OpenAI-compatible chat endpoints (incl. proxy fronts) and the
NVIDIA embeddings endpoint can fail transiently — network blip, 5xx,
429 rate-limit, openai SDK ``APIConnectionError`` / ``APITimeoutError``.
Without retry the slice is marked failed and only re-tried at the next
celery wave (5 min later, then exponential backoff). For a 20k-slice book
that means several thousand needless wave re-spins.

This module wraps a single LLM call with bounded exponential backoff so
transient blips heal in-place without bubbling up to the wave layer.
Non-retryable errors (auth, validation, 4xx other than 429) re-raise
immediately so we don't spin on permanent failures.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import AsyncIterator, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LLMEmptyCompletionError(RuntimeError):
    """HTTP-200 completion with empty text while output_tokens hit max_tokens.

    Observed 2026-07-26 on OpenAI-compat relays fronting claude-*: the
    upstream burns the entire output budget on hidden thinking and returns no
    content. Treated as a transient upstream failure so it retries within the
    caller's existing attempt budget instead of surfacing as a "successful"
    empty result (which downstream turns into JSON-parse failures / all-zero
    evaluation scores / blocked chapters). A plain empty response WITHOUT the
    budget-exhausted signature is not wrapped in this error.
    """

# Defaults tuned for the slice-grain calls (style abstraction / beat
# extraction / single-text embedding). Total worst-case wall time before
# giving up: ~30s (2 + 4 + 8 + 16 + jitter), which still fits inside
# semaphore-bounded concurrency without holding the slot too long.
_DEFAULT_ATTEMPTS = 4
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_MAX_DELAY = 30.0


def _is_retryable(exc: BaseException) -> bool:
    """Return True if exc looks like a transient LLM/embedding failure.

    Recognized transient categories:
      * ``httpx`` connection / read / write / pool timeouts and network errors
      * openai SDK ``APIConnectionError`` / ``APITimeoutError`` / ``RateLimitError``
        / ``InternalServerError``
      * Generic ``TimeoutError``, ``ConnectionError``, ``OSError``
      * Anything whose string repr carries a 5xx/429 HTTP status
      * ``LLMEmptyCompletionError`` (empty text with the output budget
        exhausted — relay thinking-burn, see class docstring)

    Everything else (auth, schema validation, 4xx other than 429) is
    treated as a hard failure so we don't masquerade real bugs as flakes.
    """
    if isinstance(exc, LLMEmptyCompletionError):
        return True
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    # OSError covers many low-level socket / DNS issues without forcing
    # an httpx import dependency.
    if isinstance(exc, OSError):
        return True

    # Module-name probe avoids hard import dependency on httpx/openai
    # in this shared helper.
    cls = exc.__class__
    mod = (getattr(cls, "__module__", "") or "").lower()
    name = cls.__name__

    if mod.startswith("httpx"):
        # Treat all httpx transport-level errors as retryable; the actual
        # HTTP status check happens below via the message snippet.
        if name in {
            "ConnectError", "ConnectTimeout",
            "ReadError", "ReadTimeout",
            "WriteError", "WriteTimeout",
            "PoolTimeout", "NetworkError",
            "RemoteProtocolError", "LocalProtocolError",
            "TransportError",
        }:
            return True

    if mod.startswith("openai"):
        if name in {
            "APIConnectionError", "APITimeoutError",
            "RateLimitError", "InternalServerError",
            "APIError",  # SDK base for unclassified server errors
        }:
            return True

    # NVIDIA provider raises RuntimeError with the HTTP code in the
    # message; surface 5xx/429 as retryable, 4xx as terminal.
    s = str(exc)
    if " HTTP 5" in s or " HTTP 429" in s:
        return True

    return False


# HTTP 400 is normally terminal (malformed request, fail fast). But relay /
# aggregator fronts have been observed returning *transient* 400s when their
# upstream account pool degrades. Let 400s participate in the normal attempt
# loop, capped at this many retries per call, so transient relay 400s heal
# while genuine bad requests still give up quickly.
_BAD_REQUEST_MAX_RETRIES = 2


def _is_limited_retryable(exc: BaseException) -> bool:
    """True for HTTP 400 / openai ``BadRequestError``.

    These are retried at most ``_BAD_REQUEST_MAX_RETRIES`` times per call
    (enforced by ``_RetryBudget``), unlike ``_is_retryable`` errors which may
    consume every remaining attempt.
    """
    cls = exc.__class__
    mod = (getattr(cls, "__module__", "") or "").lower()
    if mod.startswith("openai") and cls.__name__ == "BadRequestError":
        return True
    return " HTTP 400" in str(exc)


class _RetryBudget:
    """Per-call retry admission shared by the attempt loops below."""

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        self._limited_used = 0

    def admit(self, exc: BaseException, attempt: int) -> bool:
        """Return True if this failure may be retried (mutates limited count)."""
        if attempt >= self.attempts:
            return False
        if _is_retryable(exc):
            return True
        if _is_limited_retryable(exc) and self._limited_used < _BAD_REQUEST_MAX_RETRIES:
            self._limited_used += 1
            return True
        return False


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential backoff with full jitter; bounded by ``max_delay``."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay * (0.5 + random.random() * 0.5)


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
) -> T:
    """Invoke ``fn`` with bounded exponential backoff on transient errors.

    ``fn`` must be a zero-arg async callable returning ``T``.
    ``label`` is logged on retry/failure to aid worker-log triage.
    """
    budget = _RetryBudget(attempts)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not budget.admit(exc, attempt):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "llm_retry %s attempt %d/%d failed (%s: %s); sleeping %.2fs",
                label, attempt, attempts, type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
    # Unreachable; loop either returns or raises.
    assert last_exc is not None  # for type-checkers
    raise last_exc


async def stream_with_retry(
    open_stream: Callable[[], Awaitable[AsyncIterator[T]]],
    *,
    label: str,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
) -> AsyncIterator[T]:
    """Retry stream setup + first-item acquisition; never after the first item.

    ``open_stream`` is a zero-arg async callable returning an async iterator
    (e.g. an SSE chat stream). Failures occurring BEFORE the first item has
    been yielded downstream — connection refused, immediate 4xx/5xx from the
    endpoint — retry on the same endpoint with the same backoff/admission
    policy as :func:`call_with_retry` (incl. the bounded 400 budget). Once the
    first item has been yielded, errors propagate immediately: the consumer
    has already accumulated output that cannot be rewound.

    A stream that ends before producing any item is treated as a completed
    empty response, not a failure — empty-output handling stays with the
    caller (e.g. the OpenAI-compatible empty-completion cooldown).
    """
    budget = _RetryBudget(attempts)
    for attempt in range(1, attempts + 1):
        first = None
        got_first = False
        try:
            it = aiter(await open_stream())
            try:
                first = await anext(it)
                got_first = True
            except StopAsyncIteration:
                pass
        except BaseException as exc:  # noqa: BLE001
            if not budget.admit(exc, attempt):
                raise
            delay = _backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "llm_stream_retry %s attempt %d/%d failed before first chunk "
                "(%s: %s); sleeping %.2fs",
                label, attempt, attempts, type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
            continue
        if not got_first:
            return
        yield first
        async for item in it:
            yield item
        return
