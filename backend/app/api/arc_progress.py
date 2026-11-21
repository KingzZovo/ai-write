"""Author arc decisions with stable scope and confirmed commit receipts."""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.arc_progress import ArcProgressWrite
from app.models.project import Project, Volume
from app.services.arc_progress_contract import ArcProgressError, validate_result
from app.services.arc_progress_reader import load_arc_progress, receipt_header
from app.services.arc_progress_writes import prepare_arc_progress_write
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable
from app.services.entity_sources import digest

router = APIRouter(prefix="/api/projects/{project_id}/arc-progress", tags=["arc"])
logger = logging.getLogger(__name__)


class ArcProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None = None
    expected_source: dict | None = None
    action: str
    chapter_id: str | None = None
    expected_content_text: str | None = None
    expected_title: str | None = None
    author_note: str = ""
    direction: str | None = None
    closing_note: str = ""


@router.get("")
async def arc_progress_volumes(project_id: UUID, limit: int = Query(20, ge=1, le=100),
                               cursor: UUID | None = None, db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None))) is None:
        raise HTTPException(404, detail="作品不存在或已删除。")
    latest = select(ArcProgressWrite.volume_id, func.max(ArcProgressWrite.sequence).label("sequence"))
    latest = latest.where(ArcProgressWrite.project_id == project_id)
    if cursor is not None:
        latest = latest.where(ArcProgressWrite.volume_id > cursor)
    latest = latest.group_by(ArcProgressWrite.volume_id).order_by(ArcProgressWrite.volume_id).limit(limit + 1).subquery()
    rows = (await db.execute(select(ArcProgressWrite.volume_id,
        ArcProgressWrite.result_json["volume_title"].as_string().label("historical_title"),
        Volume.id.label("current_id"), Volume.title.label("current_title"))
        .join(latest, and_(ArcProgressWrite.volume_id == latest.c.volume_id,
                          ArcProgressWrite.sequence == latest.c.sequence))
        .outerjoin(Volume, and_(Volume.id == ArcProgressWrite.volume_id, Volume.project_id == project_id))
        .where(ArcProgressWrite.project_id == project_id).order_by(ArcProgressWrite.volume_id))).all()
    return {"items": [{"volume_id": str(row.volume_id), "title": row.current_title or row.historical_title or "历史分卷",
                       "deleted": row.current_id is None} for row in rows[:limit]],
            "next_cursor": str(rows[limit-1].volume_id) if len(rows) > limit else None}


@router.get("/{volume_id}")
async def get_arc_progress(project_id: UUID, volume_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        return (await load_arc_progress(db, project_id, volume_id)).public()
    except ArcProgressError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


@router.post("/{volume_id}")
async def write_arc_progress(project_id: UUID, volume_id: UUID, body: ArcProgressBody,
                             db: AsyncSession = Depends(get_db)):
    values = body.model_dump(mode="json")
    try:
        async with asyncio.timeout(30):
            try:
                result = await prepare_arc_progress_write(db, project_id, volume_id, **values)
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await receipt_header(db, UUID(body.request_id)) if body.request_id else None
                if existing is None:
                    raise
                result = await prepare_arc_progress_write(db, project_id, volume_id, **values)
                await db.commit()
    except ArcProgressError as exc:
        await db.rollback()
        raise HTTPException(exc.status, detail=exc.detail) from None
    except ChapterOrderUnavailable as exc:
        await db.rollback()
        raise HTTPException(404, detail={"code": "arc_progress_unavailable", "message": str(exc)}) from None
    except ChapterOrderConflict as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code": "arc_progress_busy", "message": str(exc)}) from None
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.warning("Arc progress commit not confirmed (%s)", type(exc).__name__)
        raise HTTPException(503, detail={"code": "arc_progress_unconfirmed",
            "message": "本次操作尚未取得保存确认，请保留原请求，查询原回执或重试同一请求。",
            "request_id": body.request_id}) from None
    return result


@router.get("/{volume_id}/history")
async def arc_progress_history(project_id: UUID, volume_id: UUID,
                               limit: int = Query(20, ge=1, le=100), cursor: UUID | None = None,
                               db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None))) is None:
        raise HTTPException(404, detail="作品不存在或已删除。")
    scope = (ArcProgressWrite.project_id == project_id, ArcProgressWrite.volume_id == volume_id)
    query = select(ArcProgressWrite.id, ArcProgressWrite.sequence, ArcProgressWrite.action,
                   ArcProgressWrite.chapter_id, ArcProgressWrite.created_at).where(*scope)
    if cursor is not None:
        position = await db.scalar(select(ArcProgressWrite.sequence).where(*scope, ArcProgressWrite.id == cursor))
        if position is None:
            raise HTTPException(422, detail="历史记录位置无效，请重新读取。")
        query = query.where(ArcProgressWrite.sequence < position)
    rows = (await db.execute(query.order_by(ArcProgressWrite.sequence.desc()).limit(limit + 1))).all()
    return {"items": [{"request_id": str(row.id), "sequence": row.sequence, "action": row.action,
        "chapter_id": str(row.chapter_id) if row.chapter_id else None, "created_at": row.created_at.isoformat()}
        for row in rows[:limit]], "next_cursor": str(rows[limit-1].id) if len(rows) > limit else None}


@router.get("/{volume_id}/history/{request_id}")
async def arc_progress_detail(project_id: UUID, volume_id: UUID, request_id: UUID,
                              db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(ArcProgressWrite).join(Project, Project.id == ArcProgressWrite.project_id)
        .where(ArcProgressWrite.id == request_id, ArcProgressWrite.project_id == project_id,
               ArcProgressWrite.volume_id == volume_id, Project.deleted_at.is_(None))
        .execution_options(autoflush=False, populate_existing=True))
    if row is None:
        raise HTTPException(404, detail="找不到这次操作的历史回执。")
    try:
        receipt = validate_result(row)
        valid = (receipt["before_hash"] == digest(row.before_json)
                 and receipt["manuscript_snapshot_hash"] == digest(row.manuscript_json))
    except (ArcProgressError, ValueError, TypeError):
        valid = False
    # Keep damaged originals visible for review; a client must not accept these
    # as commit confirmation or use them to reconstruct current progress.
    return {"request_id": str(row.id), "receipt": row.result_json, "before": row.before_json,
            "manuscript": row.manuscript_json, "integrity_valid": valid}
