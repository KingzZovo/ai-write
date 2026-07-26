"""Pytest configuration and shared fixtures."""

import os
import hashlib

# The in-process app's auth (app/api/auth.py) reads these from os.environ at
# import time, defaulting to username "king" + a secret bcrypt hash that tests
# cannot know. Pin deterministic test creds BEFORE importing the app so login
# works offline. setdefault means a CI-provided real credential still wins.
os.environ.setdefault("AUTH_USERNAME", "king")
os.environ.setdefault("AUTH_PASSWORD", "testpass")
os.environ.setdefault("AUTH_PASSWORD_HASH", hashlib.sha256(b"testpass").hexdigest())

# chapter_quality_gate now reads CHAPTER_MAX_REWRITE_ROUNDS through
# app.config.settings at call time (2026-07-26 audit fix; previously an
# import-time os.getenv with a divergent 5 default). Settings still honors the
# env var, so keep the production value pinned here (before any app import,
# hence before Settings instantiates) for suite determinism regardless of the
# host env. setdefault means an explicit override still wins.
os.environ.setdefault("CHAPTER_MAX_REWRITE_ROUNDS", "2")
# Subproject B (multi-agent chapter pipeline) reads these at runtime. Pin them
# so suite-wide runs are deterministic; per-test monkeypatch.setenv still wins.
os.environ.setdefault("LOGIC_CRITIC_MAX_ROUNDS", "2")
os.environ.setdefault("CHAPTER_PIPELINE_ENABLED", "1")

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for API integration tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient):
    """Authenticated async client with JWT token."""
    resp = await client.post("/api/auth/login", json={
        "username": os.environ.get("AUTH_USERNAME", "admin"), "password": os.environ.get("AUTH_PASSWORD", "admin")
    })
    token = resp.json().get("token", "")
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
