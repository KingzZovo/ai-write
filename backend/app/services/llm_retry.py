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
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

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

    Everything else (auth, schema validation, 4xx other than 429) is
    treated as a hard failure so we don't masquerade real bugs as flakes.
    """
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
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable(exc) or attempt >= attempts:
                raise
            # Exponential backoff with full jitter; bounded by max_delay.
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay = delay * (0.5 + random.random() * 0.5)
            logger.warning(
                "llm_retry %s attempt %d/%d failed (%s: %s); sleeping %.2fs",
                label, attempt, attempts, type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
    # Unreachable; loop either returns or raises.
    assert last_exc is not None  # for type-checkers
    raise last_exc
