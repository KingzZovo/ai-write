"""Pipeline API — production pipeline management and execution."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.pipeline import PipelineRun
from app.services import pipeline_service
from app.services.production import control
from app.services.production.dispatch import dispatch_queued_commands

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_words: int | None = Field(default=None, ge=500, le=20000)
    user_input: str = Field(default="", max_length=20000)
    style_id: UUID | None = None
    structure_book_id: UUID | None = None
    enable_polish: bool = True
    max_attempts: int = Field(default=2, ge=1, le=5)


class PipelineCreateRequest(BaseModel):
    project_id: str
    volume_id: str | None = None
    options: PipelineOptions = Field(default_factory=PipelineOptions)


@router.post("", status_code=201)
async def create_pipeline(
    body: PipelineCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new pipeline run for a project or volume."""
    try:
        pipeline = await pipeline_service.create_pipeline(
            db, body.project_id, body.volume_id
        )
        pipeline.options_json = body.options.model_dump(mode="json", exclude_none=True)
        await db.flush()
        return await pipeline_service.get_pipeline_status(db, pipeline.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{pipeline_id}")
async def get_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get pipeline status with per-chapter details."""
    try:
        return await pipeline_service.get_pipeline_status(db, pipeline_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/project/{project_id}")
async def list_project_pipelines(
    project_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all pipeline runs for a project."""
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.project_id == str(project_id))
        .order_by(PipelineRun.created_at.desc())
        .limit(limit)
    )
    pipelines = result.scalars().all()
    statuses = []
    for p in pipelines:
        statuses.append(await pipeline_service.get_pipeline_status(db, p.id))
    return statuses


async def _command(db, pipeline_id, action):
    if await db.get(PipelineRun, pipeline_id) is None:
        raise HTTPException(status_code=404, detail="整书任务不存在")
    try:
        result = await action(db, pipeline_id)
        await db.commit()
        return result
    except control.PipelineConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{pipeline_id}/start")
async def start_pipeline(
    pipeline_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persist before dispatch; Beat retries even if this API process exits."""
    await _command(db, pipeline_id, control.request_start)
    background_tasks.add_task(dispatch_queued_commands, str(pipeline_id))
    return await pipeline_service.get_pipeline_status(db, pipeline_id)


@router.post("/{pipeline_id}/pause")
async def pause_pipeline(pipeline_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    await _command(db, pipeline_id, control.request_pause)
    return await pipeline_service.get_pipeline_status(db, pipeline_id)


@router.post("/{pipeline_id}/rollback")
async def rollback_pipeline(pipeline_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    await _command(db, pipeline_id, control.request_rollback)
    return await pipeline_service.get_pipeline_status(db, pipeline_id)
