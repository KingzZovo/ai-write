"""Source-checked author adoption and editing of volume outlines."""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.outline_write import VolumeOutlineWrite
from app.models.project import Project
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable
from app.services.volume_outline_writes import (
    VolumeOutlineError, prepare_volume_outline_write, read_volume_candidate, read_volume_outline,
)

router = APIRouter(prefix='/api/projects/{project_id}/volume-outlines', tags=['outlines'])
logger = logging.getLogger(__name__)


class VolumeCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: UUID
    text: str


class VolumeOutlineSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID | None = None
    expected_source: dict | None = None
    text: str | None = None
    content_json: dict | None = None
    candidate: VolumeCandidate | None = None


@router.get('/candidates/{task_id}')
async def preview_candidate(project_id: UUID, task_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        candidate = await read_volume_candidate(db, project_id, task_id)
        source = await read_volume_outline(db, project_id, candidate['volume_id'])
        if source['volume_idx'] != candidate['volume_idx']:
            raise VolumeOutlineError('volume_candidate_changed', '分卷在读取期间已调整，请重新核对。')
        return {'source': source, 'candidate': {'task_id': candidate['task_id'], 'text': candidate['text']}}
    except VolumeOutlineError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


@router.get('/{volume_id}')
async def get_volume_outline(project_id: UUID, volume_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        return await read_volume_outline(db, project_id, volume_id)
    except VolumeOutlineError as exc:
        raise HTTPException(exc.status, detail=exc.detail) from None


@router.put('/{volume_id}')
async def save_volume_outline(project_id: UUID, volume_id: UUID, body: VolumeOutlineSave,
                              db: AsyncSession = Depends(get_db)):
    values = body.model_dump(mode='json')
    try:
        async with asyncio.timeout(30):
            try:
                result = await prepare_volume_outline_write(db, project_id, volume_id, **values)
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await db.get(VolumeOutlineWrite, body.request_id, populate_existing=True)
                if existing is None: raise
                result = await prepare_volume_outline_write(db, project_id, volume_id, **values)
                await db.commit()
    except VolumeOutlineError as exc:
        await db.rollback()
        raise HTTPException(exc.status, detail=exc.detail) from None
    except ChapterOrderUnavailable as exc:
        await db.rollback()
        raise HTTPException(404, detail={'code': 'volume_outline_unavailable', 'message': str(exc)}) from None
    except ChapterOrderConflict as exc:
        await db.rollback()
        raise HTTPException(409, detail={'code': 'volume_outline_busy', 'message': str(exc)}) from None
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.warning('Volume outline write not confirmed (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'volume_outline_save_unconfirmed',
            'message': '卷纲保存尚未确认，请保留编辑稿和原请求，再核对同一请求。',
            'request_id': str(body.request_id) if body.request_id else None}) from None
    return result


@router.get('/{volume_id}/history')
async def volume_outline_history(project_id: UUID, volume_id: UUID,
                                 limit: int = Query(20, ge=1, le=100), cursor: UUID | None = None,
                                 db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None)))
    if project is None:
        raise HTTPException(404, detail='作品不存在或已删除。')
    scope = (VolumeOutlineWrite.project_id == project_id, VolumeOutlineWrite.volume_id == volume_id)
    query = select(VolumeOutlineWrite.id, VolumeOutlineWrite.created_at).where(*scope)
    if cursor is not None:
        anchor = (await db.execute(select(VolumeOutlineWrite.id, VolumeOutlineWrite.created_at)
            .where(*scope, VolumeOutlineWrite.id == cursor))).one_or_none()
        if anchor is None:
            raise HTTPException(422, detail='历史记录位置无效，请重新读取。')
        query = query.where(or_(VolumeOutlineWrite.created_at < anchor.created_at,
            and_(VolumeOutlineWrite.created_at == anchor.created_at, VolumeOutlineWrite.id < anchor.id)))
    rows = (await db.execute(query.order_by(VolumeOutlineWrite.created_at.desc(), VolumeOutlineWrite.id.desc())
        .limit(limit + 1))).all()
    return {'items': [{'request_id': str(row.id), 'created_at': row.created_at.isoformat()} for row in rows[:limit]],
            'next_cursor': str(rows[limit - 1].id) if len(rows) > limit else None}


@router.get('/{volume_id}/history/{request_id}')
async def volume_outline_write_detail(project_id: UUID, volume_id: UUID, request_id: UUID,
                                     db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(VolumeOutlineWrite).join(Project, Project.id == VolumeOutlineWrite.project_id)
        .where(VolumeOutlineWrite.id == request_id, VolumeOutlineWrite.project_id == project_id,
               VolumeOutlineWrite.volume_id == volume_id, Project.deleted_at.is_(None))
        .execution_options(populate_existing=True, autoflush=False))
    if row is None:
        raise HTTPException(404, detail='找不到这份卷纲保存历史。')
    return {'request_id': str(row.id), 'before': row.before_json, 'receipt': row.result_json}
