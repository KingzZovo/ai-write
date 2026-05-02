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
    traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05"))

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
        # Never break Sentry capture on a redactor bug -- prefer over-redaction.
        return event
