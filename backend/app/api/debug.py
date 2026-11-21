"""Debug-only endpoints.

All routes are auth-required. Guarded by `DEBUG_ENDPOINTS_ENABLED=true` env
var; returns 404 in production-like environments where it's not set.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/debug", tags=["debug"])


def _guard() -> None:
    if os.environ.get("DEBUG_ENDPOINTS_ENABLED", "false").lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/sentry")
def trigger_sentry_test() -> dict:
    """Raise a controlled exception that should show up in Sentry (if configured)."""
    _guard()
    # Intentional bug to exercise Sentry capture pipeline.
    raise RuntimeError("Sentry debug test exception (intentional)")
