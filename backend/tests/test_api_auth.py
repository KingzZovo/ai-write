"""Auth API integration tests."""

import os

import pytest


@pytest.mark.asyncio
async def test_login_success(client):
    resp = await client.post("/api/auth/login", json={
        "username": os.environ.get("AUTH_USERNAME", "admin"), "password": os.environ.get("AUTH_PASSWORD", "admin")
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["username"] == os.environ.get("AUTH_USERNAME", "admin")


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    resp = await client.post("/api/auth/login", json={
        "username": os.environ.get("AUTH_USERNAME", "admin"), "password": "wrong"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_no_token(client, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "0")
    resp = await client.get("/api/projects")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_with_token(auth_client, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "0")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from app.db.session import get_db
    from app.main import app

    result = MagicMock()
    result.scalar_one.return_value = 0
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    async def database(): yield db
    monkeypatch.setitem(app.dependency_overrides, get_db, database)
    resp = await auth_client.get("/api/projects")
    assert resp.status_code == 200
