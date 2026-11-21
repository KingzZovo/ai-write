"""Submit, inspect and explicitly resume the same author arc request."""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.session import get_db
from app.models.arc_planning import ArcPlanningRequest
from app.services.arc_planning_contract import ArcPlanningError, match_request, request_intent
from app.services.arc_planning_dispatch import publish_arc_request
from app.services.arc_planning_requests import author_request_command, existing_request, prepare_arc_request, require_project
from app.services.arc_planning_sources import public_planning_source, read_planning_source
from app.services.arc_planning_views import STATUS_COLUMNS, detail_view, status_view
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable

router = APIRouter(prefix="/api/projects/{project_id}/arc-plans", tags=["arc"])
logger = logging.getLogger(__name__)


class ArcPlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None = None
    expected_source: dict | None = None
    mode: str
    idea: str
    background: str = ""
    core_setup: str = ""
    opening_scene: str = ""
    target_chapters: int = Field(default=20, ge=4, le=40)


async def _write_error(db, exc, request_id):
    await db.rollback()
    if isinstance(exc, ArcPlanningError):
        raise HTTPException(exc.status, detail=exc.detail) from None
    if isinstance(exc, ChapterOrderUnavailable):
        raise HTTPException(404, detail={"code": "arc_planning_unavailable", "message": str(exc)}) from None
    if isinstance(exc, ChapterOrderConflict):
        raise HTTPException(409, detail={"code": "arc_planning_busy", "message": str(exc)}) from None
    logger.warning("Arc planning request commit unconfirmed (%s)", type(exc).__name__)
    raise HTTPException(503, detail={"code": "arc_planning_unconfirmed", "request_id": request_id,
        "message": "操作未取得保存确认。请保留原编号与材料，查询原请求；不要另建编号重试。"}) from None


async def _publish(request_id, *, manual=False):
    try:
        async with asyncio.timeout(5):
            await publish_arc_request(request_id, manual=manual)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False  # Persisted request is discoverable even if publish failed.


@router.get("/source")
async def planning_source(project_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        return public_planning_source(await read_planning_source(db, project_id))
    except ArcPlanningError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


@router.post("", status_code=202)
async def submit_arc_plan(project_id: UUID, body: ArcPlanBody, db: AsyncSession = Depends(get_db)):
    try:
        async with asyncio.timeout(30):
            values = body.model_dump(mode="json")
            try:
                row, created = await prepare_arc_request(db, project_id, values)
            except IntegrityError:
                await db.rollback()
                existing = await existing_request(db, body.request_id) if body.request_id else None
                if existing is None:
                    raise
                row, created = match_request(existing, request_intent(project_id, values)), False
            result = status_view(row)
            if created:
                await db.commit()
            else:
                await db.rollback()  # Exact duplicate is read-only, including while running.
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await _write_error(db, exc, body.request_id)
    published = await _publish(body.request_id) if created else None
    return {**result, "dispatch_confirmed": published}


@router.get("")
async def list_arc_plans(project_id: UUID, limit: int = Query(20, ge=1, le=100),
                         cursor: UUID | None = None, db: AsyncSession = Depends(get_db)):
    try:
        await require_project(db, project_id)
    except ArcPlanningError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None
    query = select(*STATUS_COLUMNS).where(ArcPlanningRequest.project_id == project_id)
    if cursor is not None:
        position = await db.scalar(select(ArcPlanningRequest.id).where(
            ArcPlanningRequest.id == cursor, ArcPlanningRequest.project_id == project_id))
        if position is None:
            raise HTTPException(422, detail="规划历史位置无效，请重新读取。")
        # Compare stored timestamps to stored timestamps. A Python round trip
        # may change SQLite's fractional-second representation and repeat a
        # whole same-second page; PG also keeps its native timestamp precision.
        anchor = aliased(ArcPlanningRequest)
        query = query.join(anchor, and_(anchor.id == cursor, anchor.project_id == project_id)).where(
            or_(ArcPlanningRequest.created_at < anchor.created_at,
                and_(ArcPlanningRequest.created_at == anchor.created_at, ArcPlanningRequest.id < anchor.id)))
    rows = (await db.execute(query.order_by(ArcPlanningRequest.created_at.desc(), ArcPlanningRequest.id.desc())
        .limit(limit + 1).execution_options(autoflush=False))).all()
    return {"items": [status_view(row) for row in rows[:limit]],
            "next_cursor": str(rows[limit-1].id) if len(rows) > limit else None}


async def _get_request(db, project_id, request_id, *, detail):
    try:
        await require_project(db, project_id)
    except ArcPlanningError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None
    query = select(ArcPlanningRequest) if detail else select(*STATUS_COLUMNS)
    result = await db.execute(query.where(ArcPlanningRequest.id == request_id,
        ArcPlanningRequest.project_id == project_id).execution_options(autoflush=False, populate_existing=True))
    row = result.scalar_one_or_none() if detail else result.one_or_none()
    if row is None:
        raise HTTPException(404, detail="找不到这份原规划请求。")
    return row


@router.get("/{request_id}")
async def get_arc_plan(project_id: UUID, request_id: UUID, db: AsyncSession = Depends(get_db)):
    return status_view(await _get_request(db, project_id, request_id, detail=False))


@router.get("/{request_id}/detail")
async def get_arc_plan_detail(project_id: UUID, request_id: UUID, db: AsyncSession = Depends(get_db)):
    return detail_view(await _get_request(db, project_id, request_id, detail=True))


async def _command(db, project_id, request_id, action):
    try:
        async with asyncio.timeout(30):
            row, dispatch = await author_request_command(db, project_id, request_id, action)
            result = status_view(row)
            await db.commit()
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await _write_error(db, exc, str(request_id))
    published = await _publish(str(request_id), manual=True) if dispatch else None
    return {**result, "dispatch_confirmed": published}


@router.post("/{request_id}/resume", status_code=202)
async def resume_arc_plan(project_id: UUID, request_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _command(db, project_id, request_id, "resume")


@router.post("/{request_id}/cancel")
async def cancel_arc_plan(project_id: UUID, request_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _command(db, project_id, request_id, "cancel")
