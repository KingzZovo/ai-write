"""Async generation task API: extended project list + cancel endpoint.

Covers the contract for:
  - GET  /api/generate/async/project/{project_id}?limit=&status=
      * limit (1-100, default 20), status filter, created_at desc ordering
      * new fields: progress_text (last 200 chars), error_message,
        updated_at, chapter_id (from params_json)
  - POST /api/generate/async/{task_id}/cancel
      * 200 happy path, 404 unknown, 409 task_already_terminal
  - Marker-derived cancelled status: a row whose status column was
    overwritten after cancel (by the non-cancel-aware Celery worker) must
    still be reported as 'cancelled' by both GET endpoints because
    params_json['cancelled_at'] persists.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.db.session import async_session_factory
from app.models.generation_task import GenerationTask


async def _make_task(project_id: str, **overrides) -> str:
    """Insert a GenerationTask row directly; returns its id as str."""
    fields = dict(
        project_id=project_id,
        task_type="chapter",
        status="running",
        progress_text="",
        params_json={},
        char_count=0,
    )
    fields.update(overrides)
    async with async_session_factory() as db:
        task = GenerationTask(**fields)
        db.add(task)
        await db.commit()
        return str(task.id)


async def _get_row(task_id: str) -> GenerationTask | None:
    async with async_session_factory() as db:
        return await db.get(GenerationTask, task_id)


async def _delete_project_tasks(project_id: str) -> None:
    from sqlalchemy import delete

    async with async_session_factory() as db:
        await db.execute(
            delete(GenerationTask).where(GenerationTask.project_id == project_id)
        )
        await db.commit()


@pytest_asyncio.fixture
async def project_id(auth_client):
    resp = await auth_client.post(
        "/api/projects", json={"title": "异步任务API测试", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    try:
        yield pid
    finally:
        await _delete_project_tasks(pid)
        await auth_client.delete(f"/api/projects/{pid}")


@pytest.mark.asyncio
async def test_list_new_fields_and_ordering(auth_client, project_id):
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    chapter_id = str(uuid.uuid4())
    long_progress = "字" * 300
    old_id = await _make_task(
        project_id,
        status="failed",
        error_message="boom",
        created_at=base,
    )
    new_id = await _make_task(
        project_id,
        status="running",
        progress_text=long_progress,
        params_json={"chapter_id": chapter_id},
        created_at=base + timedelta(minutes=1),
    )

    resp = await auth_client.get(f"/api/generate/async/project/{project_id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [i["task_id"] for i in items] == [new_id, old_id]  # created_at desc

    newest, oldest = items
    assert newest["progress_text"] == long_progress[-200:]
    assert len(newest["progress_text"]) == 200
    assert newest["chapter_id"] == chapter_id
    assert newest["error_message"] is None
    assert newest["updated_at"]
    assert newest["status"] == "running"

    assert oldest["error_message"] == "boom"
    assert oldest["chapter_id"] is None
    assert oldest["progress_text"] == ""


@pytest.mark.asyncio
async def test_list_limit_and_status_filter(auth_client, project_id):
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for i in range(3):
        await _make_task(
            project_id,
            status="completed" if i == 0 else "running",
            created_at=base + timedelta(minutes=i),
        )

    resp = await auth_client.get(
        f"/api/generate/async/project/{project_id}", params={"limit": 2}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await auth_client.get(
        f"/api/generate/async/project/{project_id}", params={"status": "completed"}
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["status"] == "completed"

    # limit bounds are validated (1-100)
    for bad in (0, 101):
        resp = await auth_client.get(
            f"/api/generate/async/project/{project_id}", params={"limit": bad}
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cancel_happy_path(auth_client, project_id):
    task_id = await _make_task(project_id, status="running")

    resp = await auth_client.post(f"/api/generate/async/{task_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"task_id": task_id, "status": "cancelled"}

    row = await _get_row(task_id)
    assert row.status == "cancelled"
    assert row.params_json.get("cancelled_at")  # durable marker persisted

    resp = await auth_client.get(f"/api/generate/async/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unknown_task_404(auth_client):
    resp = await auth_client.post(f"/api/generate/async/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_cancel_terminal_409(auth_client, project_id, terminal):
    task_id = await _make_task(project_id, status=terminal)

    resp = await auth_client.post(f"/api/generate/async/{task_id}/cancel")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "task_already_terminal"}


@pytest.mark.asyncio
async def test_marker_derived_cancelled_status_on_both_gets(auth_client, project_id):
    """Worker overwrote status after cancel -> GETs still report cancelled."""
    task_id = await _make_task(project_id, status="running")
    resp = await auth_client.post(f"/api/generate/async/{task_id}/cancel")
    assert resp.status_code == 200

    # Simulate the non-cancel-aware Celery worker overwriting the column.
    async with async_session_factory() as db:
        row = await db.get(GenerationTask, task_id)
        row.status = "completed"
        await db.commit()

    resp = await auth_client.get(f"/api/generate/async/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    resp = await auth_client.get(f"/api/generate/async/project/{project_id}")
    assert resp.status_code == 200
    by_id = {i["task_id"]: i for i in resp.json()}
    assert by_id[task_id]["status"] == "cancelled"

    # The status filter also honours the marker-derived status.
    resp = await auth_client.get(
        f"/api/generate/async/project/{project_id}", params={"status": "cancelled"}
    )
    assert [i["task_id"] for i in resp.json()] == [task_id]
    resp = await auth_client.get(
        f"/api/generate/async/project/{project_id}", params={"status": "completed"}
    )
    assert all(i["task_id"] != task_id for i in resp.json())

    # A second cancel now 409s (already terminal via marker).
    resp = await auth_client.post(f"/api/generate/async/{task_id}/cancel")
    assert resp.status_code == 409
