"""Smoke tests for v1.0 observability endpoints.

These avoid touching the DB/Redis/Qdrant so they can run in CI without
external services.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_version_endpoint_public(client: AsyncClient):
    """/api/version must be reachable without auth and return the 3 stamp fields."""
    r = await client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"git_sha", "git_tag", "build_time"}
    # Values are strings (possibly "unknown" when not stamped at build).
    for k in ("git_sha", "git_tag", "build_time"):
        assert isinstance(body[k], str)


@pytest.mark.asyncio
async def test_metrics_endpoint_public(client: AsyncClient):
    """/metrics must be reachable without auth and return Prometheus exposition."""
    r = await client.get("/metrics")
    assert r.status_code == 200
    text = r.text
    # Prometheus text-format has HELP/TYPE lines per metric family.
    assert "# HELP" in text
    assert "# TYPE" in text
    # Our core metric families should be registered.
    # The exported counter family is http_requests_total (plural "requests");
    # see app/observability/metrics.py.
    assert "http_requests_total" in text
    assert "http_request_duration_seconds" in text


@pytest.mark.asyncio
async def test_debug_sentry_requires_auth(client: AsyncClient):
    """/api/debug/sentry must not be reachable unauthenticated."""
    r = await client.post("/api/debug/sentry")
    assert r.status_code in (401, 403, 404)


@pytest.mark.asyncio
async def test_sentry_init_noop_without_dsn(monkeypatch):
    """init_sentry() is a no-op and returns False when SENTRY_DSN is unset."""
    from app.observability.sentry_init import init_sentry
    # Force re-eval by resetting module-private flag.
    import app.observability.sentry_init as mod
    monkeypatch.setattr(mod, "_initialized", False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry("pytest") is False
