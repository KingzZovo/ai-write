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
async def test_settings_write_endpoints_disabled_and_reads_work(auth_client):
    """v1.9 PG→Neo4j 真源迁移后的写保护契约测试。

    自 v1.9 起 Neo4j 是结构化设定实体（characters / relationships /
    world-rules）的唯一真源，Postgres settings 表只是只读投影。
    ``app/api/settings.py`` 中的 ``_write_disabled()`` 让全部遗留 PG
    写端点无条件返回 410，防止直接写 PG 造成与 Neo4j 漂移。

    本测试守护两件事：
    1. 写端点必须保持 410 —— 若有人未来把写保护打开，这里要红；
    2. 只读端点（GET 列表）必须仍正常 200。
    """
    from uuid import uuid4

    # Project endpoints themselves are still writable.
    resp = await auth_client.post("/api/projects", json={"title": "写保护契约测试", "genre": "测试"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    try:
        fake_id = str(uuid4())
        rel_body = {
            "source_id": str(uuid4()),
            "target_id": str(uuid4()),
            "rel_type": "rival",
            "label": "宿敌",
            "sentiment": "negative",
        }
        # Schema-valid requests so FastAPI body validation (422) doesn't
        # mask the 410 raised by _write_disabled().
        write_attempts = [
            ("POST", f"/api/projects/{pid}/characters", {"name": "甲"}),
            ("PUT", f"/api/projects/{pid}/characters/{fake_id}", {"name": "乙"}),
            ("DELETE", f"/api/projects/{pid}/characters/{fake_id}", None),
            ("POST", f"/api/projects/{pid}/relationships", rel_body),
            ("POST", f"/api/projects/{pid}/relationships/bulk", {"items": [rel_body]}),
            ("PUT", f"/api/projects/{pid}/relationships/{fake_id}", {"label": "死敌"}),
            ("DELETE", f"/api/projects/{pid}/relationships/{fake_id}", None),
            ("POST", f"/api/projects/{pid}/world-rules", {"category": "magic_system", "rule_text": "测试规则"}),
        ]
        for method, url, body in write_attempts:
            if body is None:
                resp = await auth_client.request(method, url)
            else:
                resp = await auth_client.request(method, url, json=body)
            assert resp.status_code == 410, f"{method} {url} -> {resp.status_code}: {resp.text}"
            assert "Write endpoints disabled" in resp.json()["detail"]

        # Read-only projections must keep working (empty list is fine).
        resp = await auth_client.get(f"/api/projects/{pid}/characters")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        resp = await auth_client.get(f"/api/projects/{pid}/relationships")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
    finally:
        # Hard-delete so no test residue is left in the dev database.
        resp = await auth_client.delete(f"/api/projects/{pid}?purge=true")
        assert resp.status_code == 204
