"""API tests for the author dossier endpoints (app/api/decompile.py).

POST /api/decompile/authors/consolidate — 202 + queued status marker,
celery-first with in-process background fallback (celery always faked).
GET  /api/decompile/authors/dossier?author=... — dossier + status, 404 when
the author has no reference books.
GET  /api/decompile/authors — distinct authors with readiness counts.

Author names are Chinese and travel in the query string / JSON body — the
routes must also not be shadowed by the /api/decompile/{book_id} UUID routes.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select, text

from app.db.session import async_session_factory
from app.models.author_dossier import AuthorDossier
from app.models.project import ReferenceBook

# DDL mirrors alembic a1001920 exactly (idempotent).
_AUTHOR_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS author_dossiers (
  id uuid PRIMARY KEY,
  author varchar(200) NOT NULL UNIQUE,
  status_json jsonb,
  dossier_json jsonb,
  source_book_ids_json jsonb,
  created_at timestamptz,
  updated_at timestamptz
);
"""


async def _ensure_author_table() -> None:
    async with async_session_factory() as db:
        await db.execute(text(_AUTHOR_TABLE_DDL))
        await db.commit()


async def _create_book(author: str, title: str = "作者接口测试书", **extra) -> str:
    async with async_session_factory() as db:
        book = ReferenceBook(
            title=title, author=author, source="upload_txt", status="ready",
            **extra,
        )
        db.add(book)
        await db.commit()
        return str(book.id)


async def _cleanup_author(author: str) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(ReferenceBook).where(ReferenceBook.author == author))
        await db.execute(delete(AuthorDossier).where(AuthorDossier.author == author))
        await db.commit()


async def _load_row(author: str) -> AuthorDossier | None:
    async with async_session_factory() as db:
        result = await db.execute(
            select(AuthorDossier).where(AuthorDossier.author == author)
        )
        return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_author_consolidate_404_for_unknown_author(auth_client) -> None:
    await _ensure_author_table()
    resp = await auth_client.post(
        "/api/decompile/authors/consolidate",
        json={"author": "查无此人-接口测试"},
    )
    assert resp.status_code == 404
    resp_blank = await auth_client.post(
        "/api/decompile/authors/consolidate", json={"author": "  "}
    )
    assert resp_blank.status_code == 404


@pytest.mark.asyncio
async def test_author_dossier_404_for_unknown_author(auth_client) -> None:
    await _ensure_author_table()
    resp = await auth_client.get(
        "/api/decompile/authors/dossier", params={"author": "查无此人-接口测试"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_author_consolidate_queues_celery_and_marks_status(
    auth_client, monkeypatch
) -> None:
    from types import SimpleNamespace

    sent: list[tuple] = []

    def fake_send_task(name, args=None, **kwargs):
        sent.append((name, args))
        return SimpleNamespace(id="fake-author-task-1")

    monkeypatch.setattr("app.tasks.celery_app.send_task", fake_send_task)

    author = "接口作者甲"
    await _ensure_author_table()
    await _create_book(author)
    try:
        resp = await auth_client.post(
            "/api/decompile/authors/consolidate", json={"author": author}
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["task_id"] == "fake-author-task-1"
        assert sent == [("tasks.consolidate_author", [author])]

        row = await _load_row(author)
        assert row is not None
        assert row.status_json["state"] == "queued"
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_author_consolidate_falls_back_to_background_asyncio(
    auth_client, monkeypatch
) -> None:
    def broken_send_task(*args, **kwargs):
        raise RuntimeError("broker down")

    ran: list[str] = []

    async def fake_consolidate_author(author, db=None):
        ran.append(str(author))
        return {"status": "done"}

    monkeypatch.setattr("app.tasks.celery_app.send_task", broken_send_task)
    monkeypatch.setattr(
        "app.services.book_dossier.consolidate_author", fake_consolidate_author
    )

    author = "接口作者乙"
    await _ensure_author_table()
    await _create_book(author)
    try:
        resp = await auth_client.post(
            "/api/decompile/authors/consolidate", json={"author": author}
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "started"

        for _ in range(20):
            if ran:
                break
            await asyncio.sleep(0.01)
        assert ran == [author]

        row = await _load_row(author)
        assert row.status_json["state"] == "queued"  # marker written before fire
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_get_author_dossier_returns_stored_payload(auth_client) -> None:
    author = "接口作者丙"
    dossier = {
        "style_block": "【风格档案】\n视角：第三人称",
        "structure_block": "【剧情架构】\n宏观结构：三卷式",
        "world_block": "【世界观架构】\n力量体系：九层",
        "style_data": {"profile": {}, "cross_book": {}, "book_labels": {}},
        "plot_data": {"profile": {}, "cross_book": {}, "book_labels": {}},
        "world_data": {"profile": {}, "book_labels": {}},
        "consolidated_at": "2026-07-26T00:00:00+00:00",
        "source_counts": {"books": 2, "style_cards": 12, "beat_cards": 12},
    }
    await _ensure_author_table()
    await _create_book(author)
    async with async_session_factory() as db:
        db.add(AuthorDossier(
            author=author,
            status_json={"state": "done", "llm_calls": 3},
            dossier_json=dossier,
            source_book_ids_json=[],
        ))
        await db.commit()
    try:
        resp = await auth_client.get(
            "/api/decompile/authors/dossier", params={"author": author}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["author"] == author
        assert body["status"]["state"] == "done"
        assert body["dossier"] == dossier
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_get_author_dossier_empty_before_consolidation(auth_client) -> None:
    author = "接口作者丁"
    await _ensure_author_table()
    await _create_book(author)
    try:
        resp = await auth_client.get(
            "/api/decompile/authors/dossier", params={"author": author}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["author"] == author
        assert body["status"] is None
        assert body["dossier"] is None
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_list_authors_counts_and_status(auth_client) -> None:
    author = "接口作者戊"
    await _ensure_author_table()
    await _create_book(author, title="戊书一号", metadata_json={
        "dossier": {"style_block": "x"},
        "dossier_status": {"state": "done", "llm_calls": 5},
    })
    await _create_book(author, title="戊书二号")
    async with async_session_factory() as db:
        db.add(AuthorDossier(
            author=author,
            status_json={"state": "done", "llm_calls": 3},
            dossier_json={},
            source_book_ids_json=[],
        ))
        await db.commit()
    try:
        resp = await auth_client.get("/api/decompile/authors")
        assert resp.status_code == 200, resp.text
        entries = {e["author"]: e for e in resp.json()}
        entry = entries[author]
        assert entry["book_count"] == 2
        assert entry["books_with_dossier"] == 1
        assert entry["dossier_status"]["state"] == "done"
    finally:
        await _cleanup_author(author)


@pytest.mark.asyncio
async def test_book_id_routes_not_shadowed_by_author_routes(auth_client) -> None:
    """Registration-order guard: /authors must win over /{book_id}, and the
    UUID routes must keep working."""
    import uuid as _uuid

    await _ensure_author_table()
    # "authors" segment must NOT be parsed as a book UUID (would be 422)
    resp = await auth_client.post(
        "/api/decompile/authors/consolidate", json={"author": "无此作者-shadow"}
    )
    assert resp.status_code == 404  # reached the author route, not 422

    resp2 = await auth_client.get(f"/api/decompile/{_uuid.uuid4()}/dossier")
    assert resp2.status_code == 404  # UUID route still resolves normally
