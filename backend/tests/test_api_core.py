"""Core API integration tests — projects, styles, prompts, filter-words."""

import pytest


@pytest.mark.asyncio
async def test_projects_crud(auth_client):
    # Create
    resp = await auth_client.post("/api/projects", json={
        "title": "pytest测试项目", "genre": "玄幻"
    })
    assert resp.status_code == 200
    project = resp.json()
    pid = project["id"]

    # List
    resp = await auth_client.get("/api/projects")
    assert resp.status_code == 200
    assert any(p["id"] == pid for p in resp.json()["projects"])

    # Delete
    resp = await auth_client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_styles_crud(auth_client):
    # Create
    resp = await auth_client.post("/api/styles", json={
        "name": "pytest风格", "rules_json": [{"rule": "测试规则", "weight": 0.8, "category": "test"}]
    })
    assert resp.status_code == 201
    style = resp.json()
    sid = style["id"]

    # List
    resp = await auth_client.get("/api/styles")
    assert resp.status_code == 200
    assert any(s["id"] == sid for s in resp.json())

    # Delete
    resp = await auth_client.delete(f"/api/styles/{sid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_prompts_list(auth_client):
    resp = await auth_client.get("/api/prompts")
    assert resp.status_code == 200
    prompts = resp.json()
    assert len(prompts) >= 9  # 9 builtins


@pytest.mark.asyncio
async def test_filter_words_list(auth_client):
    resp = await auth_client.get("/api/filter-words")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 30


@pytest.mark.asyncio
async def test_model_config(auth_client):
    resp = await auth_client.get("/api/model-config/endpoints")
    assert resp.status_code == 200
    data = resp.json()
    assert "endpoints" in data


@pytest.mark.asyncio
async def test_rankings(auth_client):
    resp = await auth_client.get("/api/knowledge/rankings")
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
