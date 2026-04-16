"""Auth API integration tests."""

import pytest


@pytest.mark.asyncio
async def test_login_success(client):
    resp = await client.post("/api/auth/login", json={
        "username": "king", "password": "Wt991125"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["username"] == "king"


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    resp = await client.post("/api/auth/login", json={
        "username": "king", "password": "wrong"
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_no_token(client):
    resp = await client.get("/api/projects")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_with_token(auth_client):
    resp = await auth_client.get("/api/projects")
    assert resp.status_code == 200
