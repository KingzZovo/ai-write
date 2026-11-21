from __future__ import annotations

import pytest
import re
from unittest.mock import AsyncMock
from uuid import uuid4
from tests.support.arc_outline import arc_outline
pytestmark = pytest.mark.external_services


@pytest.fixture(autouse=True)
def staged_arc_model(monkeypatch):
    """Keep the old case-specific model fixtures behind the real durable worker."""
    from app.api import arc as arc_api, arc_planning
    from app.services import arc_planning_execution as execution
    from tests.test_arc_planning_journal import route
    plans = {}
    async def capture(task, db): return route(task)
    async def stage(task, user, db, **kwargs):
        pid = str(kwargs["project_id"])
        target = int(re.search(r"本弧准确规划 (\d+) 章", user).group(1))
        if task == "arc_outline_meta":
            plan = await arc_api.generate_arc_outline(target_chapters=target)
            plans[pid] = plan
            if not plan.get("available"):
                return {"parse_error": True, "raw_text": "原模型未返回完整规划"}
            return {key: value for key, value in plan.items() if key not in {"available", "arc_plan_version", "chapter_summaries"}}
        start, end = map(int, re.search(r"start=(\d+), end=(\d+)", user).groups())
        return {"chapter_summaries": plans[pid]["chapter_summaries"][start-1:end]}
    monkeypatch.setattr(execution, "capture_prompt_snapshot", capture)
    monkeypatch.setattr(execution, "run_structured_prompt", stage)
    monkeypatch.setattr(arc_planning, "publish_arc_request", AsyncMock())


async def plan_arc(client, pid, material, endpoint="start"):
    """Return the actual committed detail response, rather than a synchronous plan."""
    from app.services.arc_planning_execution import run_arc_request
    source = await client.get(f"/api/projects/{pid}/arc-plans/source")
    if source.status_code != 200:
        return source
    rid = str(uuid4())
    response = await client.post(f"/api/arc/{pid}/{endpoint}", json={**material,
        "request_id": rid, "expected_source": source.json()["source"]})
    assert response.status_code == 202, response.text
    await run_arc_request(rid)
    return await client.get(f"/api/projects/{pid}/arc-plans/{rid}/detail")


async def decision(client, pid, vid, action, **values):
    url = f"/api/projects/{pid}/arc-progress/{vid}"
    source = (await client.get(url)).json()["source"]
    return await client.post(url, json={"request_id": str(uuid4()), "expected_source": source,
                                       "action": action, **values})


async def confirm_saved(client, pid, vid, cid, text, note):
    url = f"/api/projects/{pid}/chapters/{cid}/content"
    source = (await client.get(url)).json()
    saved = await client.put(url, json={"expected_content_text": source["content_text"], "content_text": text})
    assert saved.status_code == 200, saved.text
    current = (await client.get(url)).json()
    return await decision(client, pid, vid, "acknowledge", chapter_id=cid,
                          expected_content_text=current["content_text"], expected_title=current["title"], author_note=note)


@pytest.mark.asyncio
async def test_start_arc_creates_volume_and_outline(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return arc_outline(kwargs["target_chapters"], title="边境小城御敌")

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧测试", "genre": "玄幻"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    try:
        r = await plan_arc(auth_client, pid, {
            "idea": "玄幻穿越边境小城",
            "background": "功法体系XX 战力YY",
            "core_setup": "主角有大敌",
            "opening_scene": "有人上门找茬",
            "target_chapters": 20,
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        body = r.json()["result"]
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
        r = await plan_arc(auth_client, pid, {
            "idea": "x", "background": "y", "core_setup": "z", "opening_scene": "w",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "needs_review"
        assert r.json()["result"] is None
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.json()["arc"] is None
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_next_direction_and_chapter_brief(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return arc_outline(kwargs["target_chapters"], title="边境御敌")

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧方向", "genre": "x"})
    pid = resp.json()["id"]
    try:
        start = await plan_arc(auth_client, pid, {
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })

        vid = start.json()["result"]["volume_id"]
        progress = (await auth_client.get(f"/api/projects/{pid}/arc-progress/{vid}")).json()
        cid = progress["chapters"][0]["id"]
        w = await confirm_saved(auth_client, pid, vid, cid, "主角穿越遇挑衅。", "第1章：主角穿越遇挑衅。")
        assert w.status_code == 200, w.text
        assert w.json()["progress"]["phase"] == "awaiting_direction"
        assert len(w.json()["progress"]["acknowledged"]) == 1

        d = await decision(auth_client, pid, vid, "direction", direction="主角发现跑不了，狐假虎威")
        assert d.status_code == 200
        assert d.json()["progress"]["phase"] == "active"
        assert d.json()["progress"]["direction"] == "主角发现跑不了，狐假虎威"

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
        return arc_outline(kwargs["target_chapters"], title="弧一")

    async def fake_suggest(**kwargs):
        return ["进城拜师", "仇家追杀", "捡到秘籍"]

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)
    monkeypatch.setattr(arc_mod, "build_arc_completion_suggestions", fake_suggest)

    resp = await auth_client.post("/api/projects", json={"title": "弧完结", "genre": "x"})
    pid = resp.json()["id"]
    try:
        start = await plan_arc(auth_client, pid, {
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 4,
        })
        vid = start.json()["result"]["volume_id"]
        progress = (await auth_client.get(f"/api/projects/{pid}/arc-progress/{vid}")).json()
        for i, chapter in enumerate(progress["chapters"], 1):
            w2 = await confirm_saved(auth_client, pid, vid, chapter["id"], f"第{i}章完整保存稿。", f"第{i}章")
            assert w2.status_code == 200, w2.text
        assert w2.json()["progress"]["phase"] == "completed"
        assert not w2.json()["progress"]["closed_early"]
        assert (await auth_client.post(f"/api/arc/{pid}/complete")).status_code == 428

        n = await plan_arc(auth_client, pid, {
            "idea": "i2", "background": "b", "core_setup": "新威胁",
            "opening_scene": "进城遇贵人", "target_chapters": 20,
        }, endpoint="next-arc")
        assert n.status_code == 200, n.text
        assert n.json()["status"] == "completed"
        assert n.json()["result"]["volume_idx"] == 2
        assert n.json()["result"]["arc"]["status"] == "active"
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_next_arc_blocked_when_current_not_completed(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return arc_outline(kwargs["target_chapters"], title="弧一")

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧守卫", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await plan_arc(auth_client, pid, {
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })
        n = await plan_arc(auth_client, pid, {
            "idea": "i2", "background": "b", "core_setup": "c2", "opening_scene": "o2",
        }, endpoint="next-arc")
        assert n.status_code == 409
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
