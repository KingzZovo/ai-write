"""Core API integration tests — projects, styles, prompts, filter-words."""

import pytest


@pytest.mark.asyncio
async def test_projects_crud(auth_client):
    # Create
    resp = await auth_client.post("/api/projects", json={
        "title": "pytest测试项目", "genre": "玄幻"
    })
    assert resp.status_code == 201
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


@pytest.mark.asyncio
async def test_soft_delete_and_restore_project(auth_client):
    # Create
    resp = await auth_client.post("/api/projects", json={"title": "软删测试", "genre": "测试"})
    assert resp.status_code in (200, 201)
    pid = resp.json()["id"]

    # Soft delete
    resp = await auth_client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204

    # Should be hidden from active list
    resp = await auth_client.get("/api/projects")
    ids = [p["id"] for p in resp.json()["projects"]]
    assert pid not in ids

    # Should appear in trashed list
    resp = await auth_client.get("/api/projects?trashed=true")
    trashed_ids = [p["id"] for p in resp.json()["projects"]]
    assert pid in trashed_ids

    # GET on soft-deleted returns 404
    resp = await auth_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404

    # Restore
    resp = await auth_client.post(f"/api/projects/{pid}/restore")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid

    # Back in active list
    resp = await auth_client.get("/api/projects")
    ids = [p["id"] for p in resp.json()["projects"]]
    assert pid in ids

    # Purge
    resp = await auth_client.delete(f"/api/projects/{pid}?purge=true")
    assert resp.status_code == 204
    resp = await auth_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_relationships_crud(auth_client):
    # Create project
    resp = await auth_client.post("/api/projects", json={"title": "关系测试", "genre": "测试"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    # Create two characters
    c1 = (await auth_client.post(f"/api/projects/{pid}/characters", json={"name": "甲"})).json()
    c2 = (await auth_client.post(f"/api/projects/{pid}/characters", json={"name": "乙"})).json()

    # Create relationship
    resp = await auth_client.post(
        f"/api/projects/{pid}/relationships",
        json={
            "source_id": c1["id"],
            "target_id": c2["id"],
            "rel_type": "rival",
            "label": "宿敌",
            "sentiment": "negative",
        },
    )
    assert resp.status_code == 201
    rid = resp.json()["id"]

    # List
    resp = await auth_client.get(f"/api/projects/{pid}/relationships")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["relationships"][0]["label"] == "宿敌"

    # Update
    resp = await auth_client.put(
        f"/api/projects/{pid}/relationships/{rid}",
        json={"label": "死敌"},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "死敌"

    # Bulk
    resp = await auth_client.post(
        f"/api/projects/{pid}/relationships/bulk",
        json={"items": [{
            "source_id": c2["id"],
            "target_id": c1["id"],
            "rel_type": "rival",
            "label": "反向",
            "sentiment": "negative",
        }]},
    )
    assert resp.status_code == 201
    assert resp.json()["created"] == 1

    # Delete
    resp = await auth_client.delete(f"/api/projects/{pid}/relationships/{rid}")
    assert resp.status_code == 204
    resp = await auth_client.get(f"/api/projects/{pid}/relationships")
    assert resp.json()["total"] == 1

    # Cleanup
    await auth_client.delete(f"/api/projects/{pid}?purge=true")
