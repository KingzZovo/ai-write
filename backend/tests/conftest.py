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

# chapter_quality_gate reads CHAPTER_MAX_REWRITE_ROUNDS via os.getenv at import
# time. In a single-file run .env (which pins 2) is loaded first; in a full-suite
# run the module imports before .env, falling back to the 5 default — so the gate
# round-count tests (which assert 2) pass alone but fail in the suite. Pin the
# production value here, before any app import, to make the suite deterministic.
# setdefault means an explicit override still wins.
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
