"""v1.7 X5: cascade_tasks read-only API surface.

Frontend uses this to render a per-project / per-chapter "cascade"
panel showing pending / running / done / failed / skipped upstream-fix
tasks that were triggered when a chapter's overall evaluation fell
below threshold.

This is intentionally read-only: cascade tasks are created by the
backend planner (`app/tasks/cascade.py`) when SSE emits
`cascade_triggered`. The UI only needs to observe and let the user
drill in.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.cascade_task import CascadeTask, STATUSES

router = APIRouter(prefix="/api/projects/{project_id}/cascade-tasks", tags=["cascade"])


class CascadeTaskResponse(BaseModel):
    id: UUID
    project_id: UUID
    source_chapter_id: UUID
    source_evaluation_id: UUID
    target_entity_type: str
    target_entity_id: UUID
    severity: str
    issue_summary: Optional[str] = None
    status: str
    parent_task_id: Optional[UUID] = None
    attempt_count: int
    error_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(cls, row: CascadeTask) -> "CascadeTaskResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            source_chapter_id=row.source_chapter_id,
            source_evaluation_id=row.source_evaluation_id,
            target_entity_type=row.target_entity_type,
            target_entity_id=row.target_entity_id,
            severity=row.severity,
            issue_summary=row.issue_summary,
            status=row.status,
            parent_task_id=row.parent_task_id,
            attempt_count=row.attempt_count,
            error_message=row.error_message,
            created_at=row.created_at.isoformat() if row.created_at else "",
            started_at=row.started_at.isoformat() if row.started_at else None,
            completed_at=row.completed_at.isoformat() if row.completed_at else None,
        )


class CascadeTaskSummary(BaseModel):
    """Aggregate counts by status for a project (or chapter)."""
    pending: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0


@router.get("", response_model=list[CascadeTaskResponse])
async def list_cascade_tasks(
    project_id: str,
    chapter_id: Optional[str] = Query(None, description="Filter by source_chapter_id"),
    status: Optional[str] = Query(None, description="Filter by status: pending|running|done|failed|skipped"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[CascadeTaskResponse]:
    """List cascade tasks for a project, newest first.

    Optional filters:
      - chapter_id: only tasks whose source chapter matches
      - status: only tasks in the given terminal/non-terminal state
    """
    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status; must be one of {STATUSES}",
        )
    q = select(CascadeTask).where(CascadeTask.project_id == project_id)
    if chapter_id:
        q = q.where(CascadeTask.source_chapter_id == chapter_id)
    if status:
        q = q.where(CascadeTask.status == status)
    q = q.order_by(CascadeTask.created_at.desc()).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [CascadeTaskResponse.from_row(r) for r in rows]


@router.get("/summary", response_model=CascadeTaskSummary)
async def cascade_tasks_summary(
    project_id: str,
    chapter_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> CascadeTaskSummary:
    """Aggregate count by status. Cheap header-strip for badges."""
    from sqlalchemy import func

    q = select(CascadeTask.status, func.count(CascadeTask.id)).where(
        CascadeTask.project_id == project_id
    )
    if chapter_id:
        q = q.where(CascadeTask.source_chapter_id == chapter_id)
    q = q.group_by(CascadeTask.status)
    result = await db.execute(q)
    summary = CascadeTaskSummary()
    for status_val, n in result.all():
        if status_val == "pending":
            summary.pending = n
        elif status_val == "running":
            summary.running = n
        elif status_val == "done":
            summary.done = n
        elif status_val == "failed":
            summary.failed = n
        elif status_val == "skipped":
            summary.skipped = n
        summary.total += n
    return summary


@router.get("/{task_id}", response_model=CascadeTaskResponse)
async def get_cascade_task(
    project_id: str,
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> CascadeTaskResponse:
    """Get a single cascade task by id."""
    row = await db.get(CascadeTask, task_id)
    if not row or str(row.project_id) != project_id:
        raise HTTPException(status_code=404, detail="cascade task not found")
    return CascadeTaskResponse.from_row(row)
