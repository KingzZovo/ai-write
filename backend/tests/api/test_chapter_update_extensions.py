"""ChapterUpdate extensions: summary edit, reorder (global_idx recompute),
and ChapterCreate.target_word_count.

``global_idx`` is stamped by the before_insert listener only; the reorder
path in api/chapters.py must recompute it (base of earlier volumes + local
idx) for the whole volume. These tests run against the live test DB via the
in-process app, like tests/api/test_export_txt.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.project import Chapter


async def _make_project(auth_client) -> str:
    resp = await auth_client.post(
        "/api/projects", json={"title": "章节更新扩展测试", "genre": "玄幻"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_volume(auth_client, pid: str, idx: int) -> str:
    resp = await auth_client.post(
        f"/api/projects/{pid}/volumes", json={"title": f"第{idx}卷", "volume_idx": idx}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _make_chapter(auth_client, pid: str, volume_id: str, idx: int, **extra) -> dict:
    resp = await auth_client.post(
        f"/api/projects/{pid}/chapters",
        json={"volume_id": volume_id, "title": f"第{idx}章", "chapter_idx": idx, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _global_idxs(chapter_ids: list[str]) -> dict[str, int | None]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Chapter.id, Chapter.global_idx).where(
                    Chapter.id.in_(chapter_ids)
                )
            )
        ).all()
    return {str(cid): gidx for cid, gidx in rows}


@pytest.mark.asyncio
async def test_summary_update_round_trip(auth_client):
    pid = await _make_project(auth_client)
    try:
        vid = await _make_volume(auth_client, pid, 1)
        ch = await _make_chapter(auth_client, pid, vid, 1)
        cid = ch["id"]

        u = await auth_client.put(
            f"/api/projects/{pid}/chapters/{cid}",
            json={"summary": "主角初入宗门，结识林昭。"},
        )
        assert u.status_code == 200, u.text
        assert u.json()["summary"] == "主角初入宗门，结识林昭。"

        g = await auth_client.get(f"/api/projects/{pid}/chapters/{cid}")
        assert g.json()["summary"] == "主角初入宗门，结识林昭。"

        # Updating an unrelated field leaves the summary untouched.
        u2 = await auth_client.put(
            f"/api/projects/{pid}/chapters/{cid}", json={"title": "新标题"}
        )
        assert u2.status_code == 200, u2.text
        assert u2.json()["summary"] == "主角初入宗门，结识林昭。"

        # Explicit null clears it.
        u3 = await auth_client.put(
            f"/api/projects/{pid}/chapters/{cid}", json={"summary": None}
        )
        assert u3.status_code == 200, u3.text
        assert u3.json()["summary"] is None
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_create_chapter_with_target_word_count(auth_client):
    pid = await _make_project(auth_client)
    try:
        vid = await _make_volume(auth_client, pid, 1)

        ch = await _make_chapter(auth_client, pid, vid, 1, target_word_count=5432)
        assert ch["target_word_count"] == 5432
        g = await auth_client.get(f"/api/projects/{pid}/chapters/{ch['id']}")
        assert g.json()["target_word_count"] == 5432

        # Omitting it still yields a positive resolved default.
        ch2 = await _make_chapter(auth_client, pid, vid, 2)
        assert isinstance(ch2["target_word_count"], int)
        assert ch2["target_word_count"] > 0
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_reorder_recomputes_global_idx_for_volume(auth_client):
    """Multi-volume: moving a volume-2 chapter recomputes global_idx for
    every chapter of volume 2 (base = volume-1 count) and leaves volume 1
    untouched."""
    pid = await _make_project(auth_client)
    try:
        v1 = await _make_volume(auth_client, pid, 1)
        v2 = await _make_volume(auth_client, pid, 2)

        c11 = await _make_chapter(auth_client, pid, v1, 1)
        c12 = await _make_chapter(auth_client, pid, v1, 2)
        c21 = await _make_chapter(auth_client, pid, v2, 1)
        c22 = await _make_chapter(auth_client, pid, v2, 2)
        c23 = await _make_chapter(auth_client, pid, v2, 3)
        ids = [c["id"] for c in (c11, c12, c21, c22, c23)]

        # Insert-time listener baseline: vol-1 base 0, vol-2 base 2.
        assert await _global_idxs(ids) == {
            c11["id"]: 1,
            c12["id"]: 2,
            c21["id"]: 3,
            c22["id"]: 4,
            c23["id"]: 5,
        }

        # Move vol-2 chapter 3 to local idx 5.
        u = await auth_client.put(
            f"/api/projects/{pid}/chapters/{c23['id']}", json={"chapter_idx": 5}
        )
        assert u.status_code == 200, u.text
        assert u.json()["chapter_idx"] == 5

        # Whole volume 2 recomputed on base 2; volume 1 unchanged.
        assert await _global_idxs(ids) == {
            c11["id"]: 1,
            c12["id"]: 2,
            c21["id"]: 3,
            c22["id"]: 4,
            c23["id"]: 7,  # 2 (vol-1 chapters) + local 5
        }

        # Reorder within volume 1 shifts nothing in volume 2 (count kept).
        u2 = await auth_client.put(
            f"/api/projects/{pid}/chapters/{c11['id']}", json={"chapter_idx": 4}
        )
        assert u2.status_code == 200, u2.text
        got = await _global_idxs(ids)
        assert got[c11["id"]] == 4
        assert got[c12["id"]] == 2
        assert got[c21["id"]] == 3
        assert got[c22["id"]] == 4
        assert got[c23["id"]] == 7
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
