"""Pipeline API — production pipeline management and execution."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.services import pipeline_service

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class PipelineCreateRequest(BaseModel):
    project_id: str
    volume_id: str | None = None


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
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all pipeline runs for a project."""
    result = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.project_id == str(project_id))
        .order_by(PipelineRun.created_at.desc())
    )
    pipelines = result.scalars().all()
    statuses = []
    for p in pipelines:
        statuses.append(await pipeline_service.get_pipeline_status(db, p.id))
    return statuses


@router.post("/{pipeline_id}/start")
async def start_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start or resume pipeline execution."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline 不存在")

    if pipeline.state == "completed":
        raise HTTPException(status_code=400, detail="Pipeline 已完成")

    # Snapshot before starting
    await pipeline_service.snapshot_pipeline(db, pipeline_id)

    # Advance state
    pipeline = await pipeline_service.advance_pipeline(db, pipeline_id)

    # Queue Celery task for actual generation
    from app.tasks.knowledge_tasks import run_pipeline_generation
    run_pipeline_generation.delay(str(pipeline_id))

    return await pipeline_service.get_pipeline_status(db, pipeline.id)


@router.post("/{pipeline_id}/pause")
async def pause_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pause a running pipeline."""
    pipeline = await db.get(PipelineRun, str(pipeline_id))
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline 不存在")

    pipeline.state = "paused"
    await db.flush()
    return await pipeline_service.get_pipeline_status(db, pipeline.id)


@router.post("/{pipeline_id}/rollback")
async def rollback_pipeline(
    pipeline_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rollback pipeline to the last snapshot."""
    success = await pipeline_service.rollback_pipeline(db, pipeline_id)
    if not success:
        raise HTTPException(status_code=400, detail="没有可用的快照")
    return await pipeline_service.get_pipeline_status(db, pipeline_id)
