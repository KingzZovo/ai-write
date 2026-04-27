"""v1.7 X5 unit + integration tests: cascade_tasks read-only API.

Covers:
  - CascadeTaskResponse.from_row mapper (pure)
  - GET /api/projects/{p}/cascade-tasks empty-list contract
  - GET /api/projects/{p}/cascade-tasks/summary all-zeros contract
  - Invalid `status` query param -> 400
  - GET /api/projects/{p}/cascade-tasks/{tid} unknown id -> 404

No DB writes; tests rely only on the conftest auth_client + an unknown
project UUID so the queries always return empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _stub_row(**overrides):
    """Build a stand-in CascadeTask-shaped object for from_row."""
    base = dict(
        id=uuid4(),
        project_id=uuid4(),
        source_chapter_id=uuid4(),
        source_evaluation_id=uuid4(),
        target_entity_type="outline",
        target_entity_id=uuid4(),
        severity="critical",
        issue_summary="missing foreshadow A1",
        status="pending",
        parent_task_id=None,
        attempt_count=0,
        error_message=None,
        created_at=datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc),
        started_at=None,
        completed_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_from_row_maps_all_fields_to_iso_strings():
    from app.api.cascade import CascadeTaskResponse

    started = datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    row = _stub_row(
        status="done",
        started_at=started,
        completed_at=completed,
        attempt_count=2,
    )

    out = CascadeTaskResponse.from_row(row)

    assert out.id == row.id
    assert out.status == "done"
    assert out.attempt_count == 2
    assert out.started_at == started.isoformat()
    assert out.completed_at == completed.isoformat()
    assert out.created_at == row.created_at.isoformat()
    assert out.error_message is None


def test_from_row_handles_none_timestamps():
    from app.api.cascade import CascadeTaskResponse

    row = _stub_row(started_at=None, completed_at=None)
    out = CascadeTaskResponse.from_row(row)
    assert out.started_at is None
    assert out.completed_at is None


@pytest.mark.asyncio
async def test_list_cascade_tasks_empty_for_unknown_project(auth_client):
    pid = str(uuid4())
    resp = await auth_client.get(f"/api/projects/{pid}/cascade-tasks")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_summary_all_zeros_for_unknown_project(auth_client):
    pid = str(uuid4())
    resp = await auth_client.get(f"/api/projects/{pid}/cascade-tasks/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "pending": 0, "running": 0, "done": 0,
        "failed": 0, "skipped": 0, "total": 0,
    }


@pytest.mark.asyncio
async def test_invalid_status_filter_returns_400(auth_client):
    pid = str(uuid4())
    resp = await auth_client.get(
        f"/api/projects/{pid}/cascade-tasks",
        params={"status": "banana"},
    )
    assert resp.status_code == 400
    assert "invalid status" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_cascade_task_unknown_id_returns_404(auth_client):
    pid = str(uuid4())
    tid = str(uuid4())
    resp = await auth_client.get(f"/api/projects/{pid}/cascade-tasks/{tid}")
    assert resp.status_code == 404
