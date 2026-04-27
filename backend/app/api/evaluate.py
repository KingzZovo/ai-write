"""v1.5.0 C2 Step D — async chapter evaluation API.

Decouples the 30-90s ChapterEvaluator LLM call from the request thread.
The sync POST /api/versions/evaluate continues to work for callers who
want a single round-trip (it blocks for the full eval); this new flow
is aimed at the UI which prefers to poll.

Endpoints:
  POST /api/evaluate/start            — enqueue eval, returns task_id
  GET  /api/evaluate/tasks/{task_id}  — poll status + result snapshot

The heavy lifting lives in ``tasks/evaluation_tasks.py``; this module
just owns the HTTP shape + DB row creation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, EvaluateTask

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])


class EvaluateStartRequest(BaseModel):
    chapter_id: str
    # Round index for telemetry: 0 = baseline, >=1 = post auto-revise.
    round_idx: int = 0
    # Free-form audit string (e.g. 'frontend_quality_panel', 'auto_revise_round1').
    caller: str = ""


class EvaluateStartResponse(BaseModel):
    task_id: str
    chapter_id: str
    status: str
    round_idx: int


class EvaluateTaskStatusResponse(BaseModel):
    task_id: str
    chapter_id: str
    status: str  # pending | running | completed | failed
    round_idx: int
    caller: str
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@router.post("/start", response_model=EvaluateStartResponse)
async def start_evaluate_task(
    body: EvaluateStartRequest,
    db: AsyncSession = Depends(get_db),
) -> EvaluateStartResponse:
    """Create a pending EvaluateTask row and dispatch the celery worker.

    Returns immediately with the new task_id. Caller polls /tasks/{id}.
    """
    chapter = await db.get(Chapter, body.chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    if not (chapter.content_text or "").strip():
        raise HTTPException(
            status_code=400, detail="Chapter has no content to evaluate"
        )

    row = EvaluateTask(
        chapter_id=str(chapter.id),
        status="pending",
        round_idx=int(body.round_idx),
        caller=str(body.caller)[:100],
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    task_id = str(row.id)
    await db.commit()

    # Dispatch AFTER commit so the worker definitely sees the row.
    from app.tasks.evaluation_tasks import dispatch_evaluate_task

    enqueued = dispatch_evaluate_task(
        evaluate_task_id=task_id,
        chapter_id=str(chapter.id),
        caller=str(body.caller) or "api.evaluate.start",
    )
    if not enqueued:
        # Surface dispatch failure but keep the row — a manual retry / sweeper
        # can pick it up later. Returning 202 would be more semantically
        # correct but the API contract is fixed at 200.
        logger.warning(
            "start_evaluate_task: dispatch failed for task %s (broker down?); row left in 'pending'",
            task_id,
        )

    return EvaluateStartResponse(
        task_id=task_id,
        chapter_id=str(chapter.id),
        status=row.status,
        round_idx=int(row.round_idx),
    )


@router.get("/tasks/{task_id}", response_model=EvaluateTaskStatusResponse)
async def get_evaluate_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> EvaluateTaskStatusResponse:
    """Poll the status + result of an EvaluateTask.

    Returns the latest snapshot. ``result`` is only populated when status
    is 'completed'; ``error`` is only populated when status is 'failed'.
    """
    row = await db.get(EvaluateTask, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Evaluate task not found")
    return EvaluateTaskStatusResponse(
        task_id=str(row.id),
        chapter_id=str(row.chapter_id),
        status=str(row.status),
        round_idx=int(row.round_idx or 0),
        caller=str(row.caller or ""),
        result=row.result_json if row.status == "completed" else None,
        error=row.error_text if row.status == "failed" else None,
        started_at=_iso(row.started_at),
        completed_at=_iso(row.completed_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )
