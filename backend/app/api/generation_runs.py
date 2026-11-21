"""v0.7 — API surface for the state-machine generation runner, Critic reports,
memory compaction, and outline-from-reference.

Routes:
  POST /api/generation-runs/start          — accept a fixed request, or read its original run
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
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.generation_run import CriticReport, GenerationRun
from app.services.generation_runner import execute_run, read_run, start_run
from app.services.generation_run_submissions import RunSubmissionError
from app.services.generation_run_views import checkpoint_payload, run_review_status, run_stop_reason
from app.services.generation_execution import (
    GenerationExecutionUnavailable, GenerationRunInvalid, GenerationRunUnavailable,
)
from app.services.production.control import PipelineConflict
from app.services.review_contract import critic_review_status
from app.services.memory_compactor import compact_project_memory
from app.services.outline_from_reference import build_outline_from_reference

logger = logging.getLogger(__name__)

router = APIRouter(tags=["v0.7"])


# ---------- Generation runs ----------


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project_id: UUID
    chapter_id: UUID
    request_id: UUID | None = None
    expected_content_text: str | None = None
    max_rewrite_count: int = Field(default=2, strict=True, ge=0, le=8)


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

    @field_validator('checkpoint_data', mode='before')
    @classmethod
    def preserve_unparsed_checkpoint(cls, value):
        return checkpoint_payload(value)

    @computed_field
    @property
    def review_status(self) -> str:
        return run_review_status(self.checkpoint_data)

    @computed_field
    @property
    def editorial_status(self) -> str:
        return 'needs_author_review'

    @computed_field
    @property
    def stop_reason(self) -> str | None:
        return run_stop_reason(self.checkpoint_data) if self.status == 'needs_review' else None


class CriticReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    round: int
    issues_json: list[dict[str, Any]]
    review_json: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def review_status(self) -> str:
        return critic_review_status({**self.review_json, 'issues': self.issues_json})


@router.post("/api/generation-runs/start", response_model=RunResponse)
async def start(req: StartRunRequest, db: AsyncSession = Depends(get_db)) -> RunResponse:
    try:
        result = await start_run(
            project_id=str(req.project_id), chapter_id=str(req.chapter_id),
            db=db, max_rewrite_count=req.max_rewrite_count,
            request_id=req.request_id, expected_content_text=req.expected_content_text,
        )
    except (PipelineConflict, GenerationRunUnavailable, GenerationRunInvalid, GenerationExecutionUnavailable, RunSubmissionError) as exc:
        raise _execution_http_error(exc) from None
    return RunResponse.model_validate(result)


@router.post("/api/generation-runs/{run_id}/resume", response_model=RunResponse)
async def resume(run_id: UUID, db: AsyncSession = Depends(get_db)) -> RunResponse:
    try:
        result = await execute_run(run_id=str(run_id), db=db, resume=True)
    except (PipelineConflict, GenerationRunUnavailable, GenerationRunInvalid, GenerationExecutionUnavailable, RunSubmissionError) as exc:
        raise _execution_http_error(exc) from None
    return RunResponse.model_validate(result)


def _execution_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RunSubmissionError):
        return HTTPException(status_code=exc.status, detail=exc.detail)
    if isinstance(exc, GenerationExecutionUnavailable):
        return HTTPException(status_code=503, detail={
            'code': 'generation_execution_unavailable', 'message': str(exc), 'run_id': exc.run_id,
        })
    if isinstance(exc, PipelineConflict):
        return HTTPException(status_code=409, detail={'code': 'generation_busy', 'message': str(exc)})
    if isinstance(exc, GenerationRunUnavailable):
        return HTTPException(status_code=404, detail={'code': 'generation_target_unavailable', 'message': str(exc)})
    return HTTPException(status_code=422, detail={'code': 'generation_input_invalid', 'message': str(exc)})


@router.get("/api/generation-runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, db: AsyncSession = Depends(get_db)) -> RunResponse:
    try:
        result = await read_run(run_id=str(run_id), db=db)
    except (GenerationRunUnavailable, GenerationExecutionUnavailable, RunSubmissionError) as exc:
        raise _execution_http_error(exc) from None
    return RunResponse.model_validate(result)


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
    request_id: UUID | None = None
    reference_book_id: UUID
    intent: str = ""
    style_hint: str = ""
    target_volumes: int = Field(default=5, strict=True, ge=1, le=2147483647)
    target_chapters_per_volume: int = Field(default=30, strict=True, ge=1, le=2147483647)


class OutlineFromReferenceTaskResponse(BaseModel):
    task_id: UUID
    project_id: UUID
    task_type: str
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
    """Use the same committed, fixed request as the general async endpoint."""
    from app.services.generation_submissions import (
        GenerationSubmissionError, submit_generation,
    )
    values = {
        'request_id': req.request_id, 'project_id': req.project_id,
        'task_type': 'outline_from_reference', 'user_input': '',
        'enable_polish': False, 'skip_polish': False,
        'params': req.model_dump(mode='json', exclude={'request_id', 'project_id'}),
    }
    try:
        return await submit_generation(db, values)
    except GenerationSubmissionError as exc:
        raise HTTPException(exc.http_status, detail=exc.detail) from None


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
    from app.services.generation_submissions import effective_generation_task_status

    task = await db.get(GenerationTask, str(task_id))
    if task is None or task.task_type != 'outline_from_reference':
        raise HTTPException(status_code=404, detail="task not found")
    params = task.params_json if isinstance(task.params_json, dict) else {}
    return OutlineFromReferenceTaskResponse(
        task_id=task.id,
        project_id=task.project_id,
        task_type=task.task_type,
        status=effective_generation_task_status(task) or "unknown",
        char_count=task.char_count or 0,
        result_text=task.result_text or task.progress_text or "",
        polished_text=task.polished_text or "",
        error_message=task.error_message,
        reference_book=params.get("reference_book"),
        sketch_line_count=params.get("sketch_line_count"),
    )
