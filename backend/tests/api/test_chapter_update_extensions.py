"""ChapterUpdate extensions: summary edit, reorder (saved book order),
and ChapterCreate.target_word_count.

Structural writes maintain ``global_idx`` by actual volume/chapter order;
sparse local labels are not added to a chapter count. These tests run against the live test DB via the
in-process app, like tests/api/test_export_txt.py.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.external_services
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
    """Sparse local labels must not create global gaps or duplicate positions."""
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

        # Saved global positions follow the complete project's actual order.
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

        # The chapter stays fifth in the book, despite its new local label.
        assert await _global_idxs(ids) == {
            c11["id"]: 1,
            c12["id"]: 2,
            c21["id"]: 3,
            c22["id"]: 4,
            c23["id"]: 5,
        }

        # Reorder within volume 1 shifts nothing in volume 2 (count kept).
        u2 = await auth_client.put(
            f"/api/projects/{pid}/chapters/{c11['id']}", json={"chapter_idx": 4}
        )
        assert u2.status_code == 200, u2.text
        got = await _global_idxs(ids)
        assert got[c11["id"]] == 2
        assert got[c12["id"]] == 1
        assert got[c21["id"]] == 3
        assert got[c22["id"]] == 4
        assert got[c23["id"]] == 5
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_explicit_author_summary_binds_to_body_and_clear_removes_stamp(auth_client):
    from app.services.summary_provenance import summary_is_current
    pid = await _make_project(auth_client)
    try:
        vid = await _make_volume(auth_client, pid, 1)
        ch = await _make_chapter(auth_client, pid, vid, 1)
        cid = ch['id']
        route = f'/api/projects/{pid}/chapters/{cid}'
        resp = await auth_client.put(route, json={'content_text': '作者正文。' * 50, 'expected_content_text': ch['content_text'] or '', 'summary': '作者写的摘要。'})
        assert resp.status_code == 200, resp.text
        async with async_session_factory() as db:
            chapter = await db.get(Chapter, cid)
            assert summary_is_current(chapter)
        resp = await auth_client.put(route, json={'content_text': '作者修改后的正文。' * 50, 'expected_content_text': resp.json()['content_text']})
        assert resp.status_code == 200, resp.text
        async with async_session_factory() as db:
            chapter = await db.get(Chapter, cid)
            assert chapter.summary == '作者写的摘要。'
            assert not summary_is_current(chapter)
        resp = await auth_client.put(route, json={'summary': None})
        assert resp.status_code == 200, resp.text
        async with async_session_factory() as db:
            assert (await db.get(Chapter, cid)).summary_source_hash is None
    finally:
        await auth_client.delete(f'/api/projects/{pid}?purge=true')
