"""Tests for the plain-text export endpoint (/api/export/projects/{id}.txt)."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_export_txt_orders_chapters_and_prefixes_headings(auth_client):
    resp = await auth_client.post(
        "/api/projects", json={"title": "txt导出测试", "genre": "玄幻"}
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    try:
        # Two volumes, created out of order to prove ordering by idx.
        v2 = await auth_client.post(
            f"/api/projects/{pid}/volumes", json={"title": "第二卷", "volume_idx": 2}
        )
        v1 = await auth_client.post(
            f"/api/projects/{pid}/volumes", json={"title": "第一卷", "volume_idx": 1}
        )
        v1_id, v2_id = v1.json()["id"], v2.json()["id"]

        async def make_chapter(volume_id: str, idx: int, title: str, content: str):
            c = await auth_client.post(
                f"/api/projects/{pid}/chapters",
                json={"volume_id": volume_id, "title": title, "chapter_idx": idx},
            )
            assert c.status_code == 201, c.text
            cid = c.json()["id"]
            u = await auth_client.put(
                f"/api/projects/{pid}/chapters/{cid}",
                json={"content_text": content},
            )
            assert u.status_code == 200, u.text

        # Bare title -> gets 第N章 prefix; prefixed title -> kept as-is.
        await make_chapter(v1_id, 1, "初入宗门", "少年推开山门。\n雾气弥漫。")
        await make_chapter(v1_id, 2, "第2章 试炼", "试炼开始了。")
        await make_chapter(v2_id, 3, "远行", "他离开了宗门。")

        r = await auth_client.get(f"/api/export/projects/{pid}.txt")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/plain")
        assert "UTF-8''" in r.headers["content-disposition"]
        assert ".txt" in r.headers["content-disposition"]

        text = r.content.decode("utf-8")
        assert "第1章 初入宗门" in text
        assert "第2章 试炼" in text
        assert "第2章 第2章" not in text  # no double prefix
        assert "第3章 远行" in text
        # Chapter order follows volume_idx then chapter_idx.
        assert (
            text.index("第1章 初入宗门")
            < text.index("第2章 试炼")
            < text.index("第3章 远行")
        )
        # Content follows its heading, paragraphs preserved.
        assert "少年推开山门。\n雾气弥漫。" in text
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_export_txt_unknown_project_404(auth_client):
    r = await auth_client.get(f"/api/export/projects/{uuid.uuid4()}.txt")
    assert r.status_code == 404
