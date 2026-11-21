"""Author cognition workflow; every acknowledgment follows its commit."""
import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.cognition_reader import CognitionNotFound, list_chapter_cognition, read_chapter_cognition, list_revisions
from app.services.cognition_requests import CognitionConflict, retry_cognition
from app.services.cognition_backfill import create_backfill, list_backfills
from app.services.cognition_edits import CognitionEditInvalid, save_cognition_decision

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/projects/{project_id}/cognition', tags=['cognition'])


class CognitionChapter(BaseModel):
    chapter_id: UUID
    chapter_title: str
    global_idx: int | None
    chapter_status: str
    status: str
    message: str
    record_id: UUID | None
    cycle_id: UUID | None
    source_hash: str | None
    version: str | None
    attempts: int
    changes_count: int | None
    can_retry: bool
    can_edit: bool
    source_mode: str
    revision_id: UUID | None


class CognitionChapterDetail(CognitionChapter):
    changes: list[dict[str, str]] | None
    model_status: str
    model_changes: list[dict[str, str]] | None
    source_content: str | None


class CognitionChapters(BaseModel):
    chapters: list[CognitionChapter]
    total: int
    has_more: bool


class CognitionRetry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    source_hash: str = Field(..., strict=True, pattern=r'^[0-9a-f]{64}$')
    version: str = Field(..., strict=True, pattern=r'^[0-9a-f]{64}$')


class CognitionBackfillRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID


class CognitionEdit(CognitionRetry):
    changes: list[dict[str, str]]


class CognitionAcknowledgment(BaseModel):
    request_id: UUID
    operation: Literal['retry', 'backfill', 'edit', 'restore_auto']
    outcome: Literal['scheduled', 'already_active', 'already_complete', 'saved']
    chapter_id: UUID | None = None
    record_id: UUID | None = None
    cycle_id: UUID | None = None
    source_hash: str | None = None
    backfill_id: UUID | None = None
    chapter_count: int | None = None
    revision_id: UUID | None = None
    source_mode: Literal['auto', 'author'] | None = None


class CognitionRevisionItem(BaseModel):
    revision_id: UUID
    previous_id: UUID | None
    source_hash: str
    mode: str
    changes: list[dict[str, str]] | None
    model_snapshot: dict | None
    actor: str | None
    created_at: datetime
    valid: bool
    matches_current_source: bool
    is_current: bool


class CognitionRevisions(BaseModel):
    revisions: list[CognitionRevisionItem]
    has_more: bool


class CognitionBackfillStatus(BaseModel):
    id: UUID
    status: str
    chapter_count: int
    cursor: int
    added_count: int
    existing_count: int
    attention_count: int
    unavailable_count: int
    updated_at: datetime


class CognitionBackfills(BaseModel):
    backfills: list[CognitionBackfillStatus]


async def _read(operation):
    try:
        return await operation
    except CognitionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception:
        logger.exception('Cognition read unavailable')
        raise HTTPException(status_code=503, detail='暂时无法读取认知记录，请稍后刷新') from None


async def _commit(db, operation):
    try:
        result = await operation
        await db.commit()
    except (CognitionNotFound, CognitionConflict, CognitionEditInvalid) as exc:
        await db.rollback()
        status = 404 if isinstance(exc, CognitionNotFound) else 422 if isinstance(exc, CognitionEditInvalid) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from None
    except Exception:
        await db.rollback()
        logger.exception('Cognition request acknowledgment unavailable')
        raise HTTPException(status_code=503, detail='尚未确认提交，请保留原请求重试') from None
    return result


@router.get('/chapters', response_model=CognitionChapters)
async def chapters(project_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                   db: AsyncSession = Depends(get_db)):
    return await _read(list_chapter_cognition(db, project_id, limit=limit, offset=offset))


@router.get('/chapters/{chapter_id}', response_model=CognitionChapterDetail)
async def chapter_detail(project_id: UUID, chapter_id: UUID, include_source: bool = False,
                         db: AsyncSession = Depends(get_db)):
    return await _read(read_chapter_cognition(db, project_id, chapter_id, include_source=include_source))


@router.post('/chapters/{chapter_id}/retry', response_model=CognitionAcknowledgment)
async def retry(project_id: UUID, chapter_id: UUID, body: CognitionRetry, db: AsyncSession = Depends(get_db)):
    return await _commit(db, retry_cognition(db, project_id, chapter_id, body.request_id, body.source_hash, body.version))


@router.post('/chapters/{chapter_id}/edit', response_model=CognitionAcknowledgment)
async def edit(project_id: UUID, chapter_id: UUID, body: CognitionEdit, request: Request,
               db: AsyncSession = Depends(get_db)):
    return await _commit(db, save_cognition_decision(db, project_id, chapter_id, body.request_id,
        body.source_hash, body.version, mode='author', changes=body.changes,
        actor=getattr(request.state, 'auth_username', None)))


@router.post('/chapters/{chapter_id}/restore-auto', response_model=CognitionAcknowledgment)
async def restore_auto(project_id: UUID, chapter_id: UUID, body: CognitionRetry, request: Request,
                       db: AsyncSession = Depends(get_db)):
    return await _commit(db, save_cognition_decision(db, project_id, chapter_id, body.request_id,
        body.source_hash, body.version, mode='auto', actor=getattr(request.state, 'auth_username', None)))


@router.get('/chapters/{chapter_id}/revisions', response_model=CognitionRevisions)
async def revisions(project_id: UUID, chapter_id: UUID, limit: int = Query(10, ge=1, le=50),
                    offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db)):
    return await _read(list_revisions(db, project_id, chapter_id, limit=limit, offset=offset))


@router.get('/backfills', response_model=CognitionBackfills)
async def backfills(project_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _read(list_backfills(db, project_id))


@router.post('/backfills', response_model=CognitionAcknowledgment)
async def backfill(project_id: UUID, body: CognitionBackfillRequest, db: AsyncSession = Depends(get_db)):
    return await _commit(db, create_backfill(db, project_id, body.request_id))
