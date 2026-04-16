"""Pytest configuration and shared fixtures."""

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
        "username": "king", "password": "Wt991125"
    })
    token = resp.json().get("token", "")
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
