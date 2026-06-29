from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_start_arc_creates_volume_and_outline(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "边境小城御敌",
                "beats": [{"chapter": 1, "beat": "主角穿越遇挑衅"}]}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧测试", "genre": "玄幻"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    try:
        r = await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "玄幻穿越边境小城",
            "background": "功法体系XX 战力YY",
            "core_setup": "主角有大敌",
            "opening_scene": "有人上门找茬",
            "target_chapters": 20,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["arc"]["title"] == "边境小城御敌"
        assert body["arc"]["status"] == "active"
        assert body["arc"]["chapters_written"] == 0
        assert body["volume_idx"] == 1

        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.status_code == 200
        assert c.json()["arc"]["title"] == "边境小城御敌"
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_current_returns_null_for_non_arc_project(auth_client):
    resp = await auth_client.post("/api/projects", json={"title": "非弧", "genre": "x"})
    pid = resp.json()["id"]
    try:
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.status_code == 200
        assert c.json()["arc"] is None
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_start_arc_rolls_back_on_outline_failure(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def failed_outline(**kwargs):
        return {"available": False}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", failed_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧失败", "genre": "x"})
    pid = resp.json()["id"]
    try:
        r = await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "x", "background": "y", "core_setup": "z", "opening_scene": "w",
        })
        assert r.status_code == 502
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.json()["arc"] is None
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
