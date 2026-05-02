"""Structured JSON logging via loguru.

Chunk-24 (v1.2.0 B-series observability).

Setup:
  - Remove loguru default stderr sink and attach a single JSON sink to stdout.
  - Intercept stdlib ``logging`` so FastAPI / uvicorn / sqlalchemy records also
    flow through loguru.
  - Redact a conservative set of sensitive keys in ``extra`` before emission.

Usage:
  from app.observability.logging import setup_logging, bind_request, log_http_request
  setup_logging()

This module is intentionally small and dependency-light so it can be imported
very early in ``app.main`` before other side-effecting imports.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

from loguru import logger as _loguru_logger

# Re-export so callers can ``from app.observability.logging import logger``.
logger = _loguru_logger

# ---------------------------------------------------------------------------
# Sensitive field masking
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "auth",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "secret_key",
        "private_key",
        "session",
        "sessionid",
    }
)

_MASK = "***"

# Bearer token regex for free-text fields.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_.=]+")


def _normalize_key(k: str) -> str:
    return k.replace("-", "_").strip().lower()


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive keys masked.

    Only walks dicts/lists/tuples; scalars are returned as-is except that
    string scalars have ``Bearer <token>`` collapsed to ``Bearer ***``.
    Recursion is bounded to avoid pathological payloads.
    """
    if _depth > 6:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _normalize_key(k) in _SENSITIVE_KEYS:
                out[k] = _MASK
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, _depth + 1) for v in value)
    if isinstance(value, str):
        return _BEARER_RE.sub(r"\1***", value)
    return value


# ---------------------------------------------------------------------------
# JSON sink
# ---------------------------------------------------------------------------
def _json_sink(message: Any) -> None:  # loguru Message
    record = message.record
    payload: dict[str, Any] = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "msg": record["message"],
    }
    # Module / line help tracing.
    if record.get("function"):
        payload["func"] = record["function"]
    if record.get("line"):
        payload["line"] = record["line"]
    # Include any bound/contextual extras (already-redacted-by-caller is fine;
    # we defensively redact again to catch stray secrets).
    extra = record.get("extra") or {}
    if extra:
        payload.update(redact(dict(extra)))
    # Exception info -> short string, keep payload single-line.
    exc = record.get("exception")
    if exc is not None:
        payload["exc_type"] = getattr(exc.type, "__name__", str(exc.type))
        payload["exc_msg"] = str(exc.value)
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        line = json.dumps({"ts": payload["ts"], "level": payload["level"],
                           "logger": payload["logger"], "msg": "<unserializable>"})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# stdlib logging -> loguru bridge
# ---------------------------------------------------------------------------
class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru so we have one sink."""

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        try:
            level = _loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        _loguru_logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


_configured = False


def setup_logging(level: str | None = None) -> None:
    """Configure loguru JSON sink + stdlib intercept. Idempotent."""
    global _configured
    if _configured:
        return
    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()

    _loguru_logger.remove()
    _loguru_logger.add(
        _json_sink,
        level=lvl,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    # Intercept stdlib logging so FastAPI / uvicorn / sqlalchemy / celery
    # records also flow through the JSON sink.
    root = logging.getLogger()
    root.handlers = [_InterceptHandler()]
    root.setLevel(lvl)
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "starlette",
        "sqlalchemy",
        "sqlalchemy.engine",
        "celery",
    ):
        lg = logging.getLogger(name)
        lg.handlers = [_InterceptHandler()]
        lg.propagate = False

    _configured = True
    _loguru_logger.info("structured logging initialized level=%s" % lvl)


def is_configured() -> bool:
    """Smoke / test helper: True once setup_logging has attached the JSON sink."""
    return _configured


# ---------------------------------------------------------------------------
# HTTP request helper
# ---------------------------------------------------------------------------
def log_http_request(
    *,
    method: str,
    path: str,
    status: int,
    latency_ms: float,
    user_id: str | None,
    request_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one structured JSON line describing an HTTP request."""
    bound = {
        "event": "http_request",
        "method": method,
        "path": path,
        "status": int(status),
        "latency_ms": round(float(latency_ms), 2),
        "user_id": user_id,
        "request_id": request_id,
    }
    if extra:
        bound.update(redact(dict(extra)))
    _loguru_logger.bind(**bound).info("http_request")
