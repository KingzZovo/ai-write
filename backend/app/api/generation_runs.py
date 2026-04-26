"""v0.7 — API surface for the state-machine generation runner, Critic reports,
memory compaction, and outline-from-reference.

Routes:
  POST /api/generation-runs/start          — create a run + kick off execution
  POST /api/generation-runs/{id}/resume    — continue an existing run
  GET  /api/generation-runs/{id}           — fetch current state + checkpoint
  GET  /api/generation-runs/{id}/reports   — list critic reports for this run
  POST /api/projects/{id}/compact-memory   — trigger memory compactor
  POST /api/outlines/from-reference        — build an outline from a reference book
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.generation_run import CriticReport, GenerationRun
from app.services.generation_runner import execute_run, start_run
from app.services.memory_compactor import compact_project_memory
from app.services.outline_from_reference import build_outline_from_reference

logger = logging.getLogger(__name__)

router = APIRouter(tags=["v0.7"])


# ---------- Generation runs ----------


class StartRunRequest(BaseModel):
    project_id: UUID
    chapter_id: UUID | None = None
    max_rewrite_count: int = 2


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    chapter_id: UUID | None
    phase: str
    status: str
    rewrite_count: int
    max_rewrite_count: int
    checkpoint_data: dict[str, Any]
    last_error: str


class CriticReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    round: int
    issues_json: list[dict[str, Any]]


@router.post("/api/generation-runs/start", response_model=RunResponse)
async def start(req: StartRunRequest, db: AsyncSession = Depends(get_db)) -> RunResponse:
    run = await start_run(
        project_id=str(req.project_id),
        chapter_id=str(req.chapter_id) if req.chapter_id else None,
        db=db,
        max_rewrite_count=req.max_rewrite_count,
    )
    # Execute synchronously for now; callers that need async can call /resume.
    await execute_run(run_id=str(run.id), db=db)
    await db.refresh(run)
    return RunResponse.model_validate(run)


@router.post("/api/generation-runs/{run_id}/resume", response_model=RunResponse)
async def resume(run_id: UUID, db: AsyncSession = Depends(get_db)) -> RunResponse:
    run = await db.get(GenerationRun, str(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    await execute_run(run_id=str(run_id), db=db, resume=True)
    await db.refresh(run)
    return RunResponse.model_validate(run)


@router.get("/api/generation-runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, db: AsyncSession = Depends(get_db)) -> RunResponse:
    run = await db.get(GenerationRun, str(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunResponse.model_validate(run)


@router.get("/api/generation-runs/{run_id}/graph", response_class=PlainTextResponse)
async def get_run_graph(run_id: UUID, db: AsyncSession = Depends(get_db)) -> str:
    """Return a DOT representation of the generation graph.

    Topology is static today (v1.0 chunk 8). Endpoint still takes run_id
    so the frontend can highlight the run's current phase later.
    """
    run = await db.get(GenerationRun, str(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    from app.graphs.generation_graph import to_dot
    return to_dot()


@router.get(
    "/api/generation-runs/{run_id}/reports",
    response_model=list[CriticReportResponse],
)
async def list_reports(run_id: UUID, db: AsyncSession = Depends(get_db)) -> list[CriticReportResponse]:
    rows = await db.execute(
        select(CriticReport)
        .where(CriticReport.run_id == str(run_id))
        .order_by(CriticReport.round.asc())
    )
    return [CriticReportResponse.model_validate(r) for r in rows.scalars().all()]


# ---------- Memory compaction ----------


class CompactResponse(BaseModel):
    status: str
    total: int | None = None
    kept_recent: int | None = None
    compacted: int | None = None
    reason: str | None = None


@router.post("/api/projects/{project_id}/compact-memory", response_model=CompactResponse)
async def compact_memory(
    project_id: UUID,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
) -> CompactResponse:
    result = await compact_project_memory(
        project_id=str(project_id),
        db=db,
        force=force,
    )
    return CompactResponse(**result)


# ---------- Outline from reference ----------


class OutlineFromReferenceRequest(BaseModel):
    reference_book_id: UUID
    project_id: UUID | None = None
    intent: str = ""
    style_hint: str = ""
    target_volumes: int = 5
    target_chapters_per_volume: int = 30


class OutlineFromReferenceResponse(BaseModel):
    status: str
    outline_text: str | None = None
    reference_book: dict[str, Any] | None = None
    sketch_line_count: int | None = None
    reason: str | None = None
    hint: str | None = None
    detail: str | None = None


@router.post(
    "/api/outlines/from-reference",
    response_model=OutlineFromReferenceResponse,
)
async def outline_from_reference(
    req: OutlineFromReferenceRequest,
    db: AsyncSession = Depends(get_db),
) -> OutlineFromReferenceResponse:
    wizard = {
        "intent": req.intent,
        "style_hint": req.style_hint,
        "target_volumes": req.target_volumes,
        "target_chapters_per_volume": req.target_chapters_per_volume,
    }
    result = await build_outline_from_reference(
        reference_book_id=str(req.reference_book_id),
        wizard_params=wizard,
        db=db,
        project_id=str(req.project_id) if req.project_id else None,
    )
    return OutlineFromReferenceResponse(**result)


# ---------- Outline from reference (async, v1.5.0 D-2) ----------
#
# The synchronous endpoint above keeps the same shape for back-compat, but in
# practice an outline_book LLM call can run for several minutes. Any reverse
# proxy or HTTP client between the browser and uvicorn that has a read timeout
# < the generation time will break the flow. The async variant below dispatches
# the work to a Celery worker (which already runs many-minute jobs cleanly via
# broker visibility_timeout=2h) and returns a task_id immediately. The frontend
# polls GET /api/outlines/from-reference/tasks/{task_id} for status + result.


class OutlineFromReferenceStartRequest(BaseModel):
    # project_id is required for the async path: GenerationTask.project_id is
    # NOT NULL, and async outline-from-reference is invoked from the project
    # creation wizard where the project already exists.
    project_id: UUID
    reference_book_id: UUID
    intent: str = ""
    style_hint: str = ""
    target_volumes: int = 5
    target_chapters_per_volume: int = 30


class OutlineFromReferenceTaskResponse(BaseModel):
    task_id: UUID
    status: str
    char_count: int = 0
    result_text: str = ""
    polished_text: str = ""
    error_message: str | None = None
    reference_book: dict[str, Any] | None = None
    sketch_line_count: int | None = None


@router.post("/api/outlines/from-reference/start")
async def outline_from_reference_start(
    req: OutlineFromReferenceStartRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a GenerationTask + dispatch celery worker; return task_id."""
    from app.models.generation_task import GenerationTask

    task = GenerationTask(
        project_id=str(req.project_id),
        task_type="outline_from_reference",
        status="pending",
        params_json={
            "reference_book_id": str(req.reference_book_id),
            "intent": req.intent,
            "style_hint": req.style_hint,
            "target_volumes": req.target_volumes,
            "target_chapters_per_volume": req.target_chapters_per_volume,
        },
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)

    from app.tasks.knowledge_tasks import run_async_generation

    run_async_generation.delay(str(task.id))

    return {"task_id": str(task.id), "status": "pending"}


@router.get(
    "/api/outlines/from-reference/tasks/{task_id}",
    response_model=OutlineFromReferenceTaskResponse,
)
async def outline_from_reference_get_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OutlineFromReferenceTaskResponse:
    """Poll an async outline-from-reference task by id."""
    from app.models.generation_task import GenerationTask

    task = await db.get(GenerationTask, str(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    params = task.params_json or {}
    return OutlineFromReferenceTaskResponse(
        task_id=task.id,
        status=task.status or "unknown",
        char_count=task.char_count or 0,
        result_text=task.result_text or task.progress_text or "",
        polished_text=task.polished_text or "",
        error_message=task.error_message,
        reference_book=params.get("reference_book"),
        sketch_line_count=params.get("sketch_line_count"),
    )
