"""API tests for the dossier consolidation endpoints (app/api/decompile.py).

POST /api/decompile/{book_id}/consolidate — 202 + dossier_status marker,
celery-first with in-process background fallback (celery is always faked
here so the live worker is never touched).
GET  /api/decompile/{book_id}/dossier — dossier + status, 404 when missing.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.db.session import async_session_factory
from app.models.project import ReferenceBook


async def _create_book(**extra) -> str:
    async with async_session_factory() as db:
        book = ReferenceBook(
            title="dossier接口测试书", source="upload_txt", status="ready", **extra
        )
        db.add(book)
        await db.commit()
        return str(book.id)


async def _delete_book(book_id: str) -> None:
    from sqlalchemy import delete

    async with async_session_factory() as db:
        await db.execute(delete(ReferenceBook).where(ReferenceBook.id == book_id))
        await db.commit()


async def _get_meta(book_id: str) -> dict:
    async with async_session_factory() as db:
        book = await db.get(ReferenceBook, book_id)
        return dict(book.metadata_json or {})


@pytest.mark.asyncio
async def test_consolidate_404_for_missing_book(auth_client) -> None:
    resp = await auth_client.post(f"/api/decompile/{uuid.uuid4()}/consolidate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dossier_404_for_missing_book(auth_client) -> None:
    resp = await auth_client.get(f"/api/decompile/{uuid.uuid4()}/dossier")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_consolidate_queues_celery_and_marks_status(auth_client, monkeypatch) -> None:
    sent: list[tuple] = []

    def fake_send_task(name, args=None, **kwargs):
        sent.append((name, args))
        return SimpleNamespace(id="fake-task-1")

    monkeypatch.setattr("app.tasks.celery_app.send_task", fake_send_task)

    book_id = await _create_book()
    try:
        resp = await auth_client.post(f"/api/decompile/{book_id}/consolidate")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["task_id"] == "fake-task-1"
        assert sent == [("tasks.consolidate_reference_book", [book_id])]

        meta = await _get_meta(book_id)
        assert meta["dossier_status"]["state"] == "queued"
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_consolidate_falls_back_to_background_asyncio(auth_client, monkeypatch) -> None:
    def broken_send_task(*args, **kwargs):
        raise RuntimeError("broker down")

    ran: list[str] = []

    async def fake_build_dossier(book_id, db=None):
        ran.append(str(book_id))
        return {"status": "done"}

    monkeypatch.setattr("app.tasks.celery_app.send_task", broken_send_task)
    monkeypatch.setattr(
        "app.services.book_dossier.build_dossier", fake_build_dossier
    )

    book_id = await _create_book()
    try:
        resp = await auth_client.post(f"/api/decompile/{book_id}/consolidate")
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "started"

        # let the background task run
        for _ in range(20):
            if ran:
                break
            await asyncio.sleep(0.01)
        assert ran == [book_id]

        meta = await _get_meta(book_id)
        assert meta["dossier_status"]["state"] == "queued"  # marker written before fire
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_get_dossier_returns_stored_payload(auth_client) -> None:
    dossier = {
        "style_block": "【风格档案】\n视角：第三人称",
        "structure_block": "【剧情架构】\n宏观结构：三卷式",
        "world_block": "【世界观架构】\n力量体系：九层",
        "style_data": {"profile": {}, "stats": {}},
        "plot_data": {"profile": {}, "stats": {}},
        "world_data": {"profile": {}, "chunks_sampled": 3},
        "consolidated_at": "2026-07-26T00:00:00+00:00",
        "source_counts": {"style_cards": 2, "beat_cards": 2, "chunks_sampled": 3},
    }
    book_id = await _create_book(metadata_json={
        "dossier": dossier,
        "dossier_status": {"state": "done", "llm_calls": 5},
    })
    try:
        resp = await auth_client.get(f"/api/decompile/{book_id}/dossier")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["book_id"] == book_id
        assert body["status"]["state"] == "done"
        assert body["dossier"] == dossier
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_get_dossier_empty_before_consolidation(auth_client) -> None:
    book_id = await _create_book()
    try:
        resp = await auth_client.get(f"/api/decompile/{book_id}/dossier")
        assert resp.status_code == 200
        body = resp.json()
        assert body["dossier"] is None
        assert body["status"] is None
    finally:
        await _delete_book(book_id)


@pytest.mark.asyncio
async def test_existing_reference_books_routes_still_served(auth_client) -> None:
    """Prefix refactor guard: the legacy /api/reference-books URLs must not move."""
    book_id = await _create_book()
    try:
        resp = await auth_client.get(f"/api/reference-books/{book_id}/decompile-status")
        assert resp.status_code == 200
        assert resp.json()["book_id"] == book_id
    finally:
        await _delete_book(book_id)
