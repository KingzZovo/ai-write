"""Focused tests for workspace persistence and ownership boundaries."""

import json

import pytest


@pytest.mark.asyncio
async def test_get_chapter_must_belong_to_project(auth_client):
    project_1 = (
        await auth_client.post("/api/projects", json={"title": "P1", "genre": "玄幻"})
    ).json()
    project_2 = (
        await auth_client.post("/api/projects", json={"title": "P2", "genre": "玄幻"})
    ).json()

    volume = (
        await auth_client.post(
            f"/api/projects/{project_1['id']}/volumes",
            json={"title": "第一卷", "volume_idx": 1},
        )
    ).json()
    chapter = (
        await auth_client.post(
            f"/api/projects/{project_1['id']}/chapters",
            json={
                "volume_id": volume["id"],
                "title": "第一章",
                "chapter_idx": 1,
                "outline_json": {},
            },
        )
    ).json()

    response = await auth_client.get(
        f"/api/projects/{project_2['id']}/chapters/{chapter['id']}"
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_outline_sse_saved_payload_uses_structured_json_when_valid(
    auth_client, monkeypatch
):
    from app.api import generate as generate_api

    payload = {
        "volume_idx": 1,
        "title": "第一卷",
        "chapter_summaries": [
            {
                "chapter_idx": 1,
                "title": "第一章",
                "summary": "开局",
                "key_events": ["出发"],
            }
        ],
    }

    async def fake_stream():
        yield json.dumps(payload, ensure_ascii=False)

    async def fake_generate_volume_outline(self, **kwargs):
        return fake_stream()

    monkeypatch.setattr(
        generate_api.OutlineGenerator,
        "generate_volume_outline",
        fake_generate_volume_outline,
    )

    project = (
        await auth_client.post("/api/projects", json={"title": "P3", "genre": "玄幻"})
    ).json()
    response = await auth_client.post(
        "/api/generate/outline",
        json={"project_id": project["id"], "level": "volume", "user_input": "test"},
    )

    assert response.status_code == 200

    outlines = await auth_client.get(f"/api/projects/{project['id']}/outlines")
    saved = outlines.json()
    volume_outline = next(item for item in saved if item["level"] == "volume")

    assert volume_outline["content_json"]["title"] == "第一卷"
    assert volume_outline["content_json"]["chapter_summaries"][0]["title"] == "第一章"
