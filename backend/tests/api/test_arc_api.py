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


@pytest.mark.asyncio
async def test_next_direction_and_chapter_brief(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "边境御敌",
                "beats": [{"chapter": 1, "beat": "穿越"}, {"chapter": 2, "beat": "对峙"}]}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧方向", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })

        w = await auth_client.post(f"/api/arc/{pid}/chapter-written", json={
            "chapter_summary": "第1章：主角穿越遇挑衅。",
        })
        assert w.status_code == 200, w.text
        assert w.json()["arc"]["status"] == "awaiting_direction"
        assert w.json()["arc"]["chapters_written"] == 1

        d = await auth_client.post(f"/api/arc/{pid}/next-direction", json={
            "direction": "主角发现跑不了，狐假虎威",
        })
        assert d.status_code == 200
        assert d.json()["arc"]["status"] == "active"
        assert d.json()["arc"]["next_direction"] == "主角发现跑不了，狐假虎威"

        b = await auth_client.get(f"/api/arc/{pid}/chapter-brief")
        assert b.status_code == 200
        brief = b.json()["brief"]
        assert "边境御敌" in brief
        assert "狐假虎威" in brief
        assert "第1章：主角穿越" in brief
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_complete_and_next_arc(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "弧一", "beats": []}

    async def fake_suggest(**kwargs):
        return ["进城拜师", "仇家追杀", "捡到秘籍"]

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)
    monkeypatch.setattr(arc_mod, "build_arc_completion_suggestions", fake_suggest)

    resp = await auth_client.post("/api/projects", json={"title": "弧完结", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 4,
        })
        # target 夹紧下限 4（spec：4–40）；写满 4 章 → completed
        await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第1章"})
        await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第2章"})
        await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第3章"})
        w2 = await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第4章"})
        assert w2.json()["arc"]["status"] == "completed"

        comp = await auth_client.post(f"/api/arc/{pid}/complete")
        assert comp.status_code == 200, comp.text
        assert len(comp.json()["arc"]["suggestions"]) == 3

        n = await auth_client.post(f"/api/arc/{pid}/next-arc", json={
            "idea": "i2", "background": "b", "core_setup": "新威胁",
            "opening_scene": "进城遇贵人", "target_chapters": 20,
        })
        assert n.status_code == 201, n.text
        assert n.json()["volume_idx"] == 2
        assert n.json()["arc"]["status"] == "active"
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_next_arc_blocked_when_current_not_completed(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "弧一", "beats": []}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧守卫", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })
        n = await auth_client.post(f"/api/arc/{pid}/next-arc", json={
            "idea": "i2", "background": "b", "core_setup": "c2", "opening_scene": "o2",
        })
        assert n.status_code == 409
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
