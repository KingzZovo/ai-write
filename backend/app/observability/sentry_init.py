"""Sentry initialization.

Idempotent; no-op when `SENTRY_DSN` env var is not set. Import and call
`init_sentry()` exactly once per process (app startup, celery worker start).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def init_sentry(component: str = "backend") -> bool:
    """Initialize Sentry SDK if SENTRY_DSN is configured.

    Returns True if initialized (or already was), False if skipped.
    """
    global _initialized
    if _initialized:
        return True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry disabled (no SENTRY_DSN set)")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.warning("sentry-sdk not installed; skipping Sentry init")
        return False

    env = os.environ.get("SENTRY_ENV", os.environ.get("APP_ENV", "dev"))
    release = os.environ.get("GIT_TAG") or os.environ.get("GIT_SHA") or "unknown"
    # 2026-11-17 审计:``float()`` 在 try 之外,环境变量写成 ``0.1x`` 之类
    # 就在启动期抛 ValueError,把整个应用带崩;这里降级为默认采样率。
    try:
        traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05"))
        if not 0.0 <= traces_rate <= 1.0:
            raise ValueError(f"out of range: {traces_rate}")
    except (TypeError, ValueError) as exc:
        logger.warning("invalid SENTRY_TRACES_SAMPLE_RATE (%s); using 0.05", exc)
        traces_rate = 0.05

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        server_name=component,
        traces_sample_rate=traces_rate,
        send_default_pii=False,
        before_send=_scrub_event,
        before_send_transaction=_scrub_event,
        integrations=[
            FastApiIntegration(),
            StarletteIntegration(),
            CeleryIntegration(),
            AsyncioIntegration(),
        ],
    )
    _initialized = True
    logger.info(
        "Sentry initialized component=%s env=%s release=%s traces=%.2f",
        component, env, release, traces_rate,
    )
    return True


# ---------------------------------------------------------------------------
# before_send / before_send_transaction redaction hook.
# Reuses the central key-list + Bearer-token regex from observability.logging
# so secret leakage policy lives in exactly one place.
# ---------------------------------------------------------------------------
def _scrub_event(event, _hint):  # type: ignore[no-untyped-def]
    try:
        from app.observability.logging import redact

        # Drop request cookies / authorization headers entirely; redact()
        # walks nested dicts/lists and masks any keys that look sensitive.
        req = event.get("request") if isinstance(event, dict) else None
        if isinstance(req, dict):
            headers = req.get("headers")
            if isinstance(headers, dict):
                req["headers"] = redact(headers)
            cookies = req.get("cookies")
            if cookies:
                req["cookies"] = "[redacted]"
            qs = req.get("query_string")
            if isinstance(qs, str) and qs:
                req["query_string"] = redact(qs)
            data = req.get("data")
            if data is not None:
                req["data"] = redact(data)
        # Top-level extra / contexts / tags get the same treatment.
        for key in ("extra", "contexts", "tags"):
            if isinstance(event, dict) and key in event and event[key] is not None:
                event[key] = redact(event[key])
        return event
    except Exception:
        # 2026-11-17 审计:注释说"prefer over-redaction",实际 return 的是**未脱敏**
        # 的 event —— redactor 一出错,request/extra 里的 query_string、api_key
        # 就原样上报。宁可不发这一条(Sentry 侧 before_send 返回 None = 丢弃)。
        logger.warning("sentry redactor failed; dropping event instead of leaking it")
        return None
