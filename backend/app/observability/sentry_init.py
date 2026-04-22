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
