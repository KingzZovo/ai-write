"""Author-managed prose associations, with existing application auth and DB."""
import asyncio
import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.entity_materialization_lock import MaterializationBusy
from app.services.foreshadow_source_link_contract import (
    SourceLinkConflict, SourceLinkNotFound, make_source_link_command,
)
from app.services.foreshadow_source_links import apply_source_link, read_source_chapters, read_source_context
from app.services.foreshadow_write_contract import ForeshadowProjectUnavailable

router = APIRouter()
logger = logging.getLogger(__name__)


class SourceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_link_id: UUID | None = Field(...)
    note: str = Field('', strict=True)


class SourceLinkRequest(SourceDecisionRequest):
    chapter_id: UUID
    source_version: str = Field(..., pattern='^[0-9a-f]{64}$', strict=True)
    evidence: str = Field(..., min_length=1, strict=True)

    @field_validator('evidence')
    @classmethod
    def nonblank_evidence(cls, value):
        if not value.strip():
            raise ValueError('请引用保存稿中的原句')
        return value


class SourceLinkEntry(BaseModel):
    id: UUID
    revision: int
    previous_id: UUID | None
    operation: Literal['link', 'unlink']
    chapter_id: UUID | None
    record_snapshot: dict
    source_snapshot: dict | None
    evidence: str | None
    note: str
    created_by: str | None
    created_at: Any


class SourcePreview(BaseModel):
    chapter_id: UUID
    title: str
    content: str
    source_snapshot: dict
    source_version: str


class SourceContextResponse(BaseModel):
    foreshadow_id: UUID
    description: str
    type: str
    record_present: bool
    record_version: str | None
    latest_link: SourceLinkEntry | None
    preview: SourcePreview | None
    history: list[SourceLinkEntry]
    has_more: bool


class SourceChapter(BaseModel):
    id: UUID
    title: str
    chapter_idx: int
    global_idx: int | None
    volume_id: UUID
    volume_title: str
    volume_idx: int


class SourceChaptersResponse(BaseModel):
    chapters: list[SourceChapter]
    has_more: bool


class SourceDecisionResponse(BaseModel):
    id: UUID
    revision: int
    operation: Literal['link', 'unlink']
    is_latest: bool


CONFLICT_MESSAGES = {
    'record_changed': '伏笔登记内容已变化，请保留输入并重新核对。',
    'link_changed': '正文关联已被其他操作修改，请保留输入并核对最新关联。',
    'chapter_unavailable': '来源章节或卷章位置不可用，请选择已保存且位置明确的章节。',
    'chapter_changed': '正文或章节位置已变化，请重新读取保存稿后核对引用。',
    'quote_invalid': '请引用保存稿中的原句。',
    'quote_missing': '保存稿中没有这段逐字引用，请重新选择。',
    'quote_ambiguous': '这段原句出现了不止一次，请扩大引用范围以明确位置。',
    'request_id_reused': '该请求编号已用于其他关联内容，请保留输入并刷新核对。',
}


async def _run(db, operation, *, write=False):
    try:
        async with asyncio.timeout(30):
            result = await operation
            if write:
                await db.commit()
            return result
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except (SourceLinkNotFound, ForeshadowProjectUnavailable):
        await db.rollback()
        raise HTTPException(404, detail={'code': 'source_unavailable', 'message': '作品或伏笔记录已不可用。'}) from None
    except SourceLinkConflict as exc:
        await db.rollback()
        raise HTTPException(409, detail={'code': exc.code,
            'message': CONFLICT_MESSAGES.get(exc.code, '关联资料已变化，请保留输入并重新核对。')}) from None
    except MaterializationBusy:
        await db.rollback()
        raise HTTPException(503, detail={'code': 'materialization_busy', 'message': '作品资料正在同步，请稍后重试。'}) from None
    except Exception as exc:
        await db.rollback()
        logger.warning('Foreshadow source operation failed (%s)', type(exc).__name__)
        code, message = ('source_link_unconfirmed', '尚未取得关联保存确认，输入已保留，请用原请求重试。') if write else (
            'source_read_unavailable', '来源资料暂时无法读取，请稍后重试。')
        raise HTTPException(503, detail={'code': code, 'message': message}) from None


@router.get('/source-chapters', response_model=SourceChaptersResponse)
async def source_chapters(project_id: UUID, q: str = Query('', max_length=100),
    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db)):
    return await _run(db, read_source_chapters(db, project_id, q.strip(), limit, offset))


@router.get('/{foreshadow_id}/source', response_model=SourceContextResponse)
async def source_context(project_id: UUID, foreshadow_id: UUID, chapter_id: UUID | None = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db)):
    return await _run(db, read_source_context(db, project_id, foreshadow_id, chapter_id, limit=limit, offset=offset))


async def _decide(project_id, foreshadow_id, body, request, db, request_id, expected_version, operation):
    command = make_source_link_command(request_id, project_id, foreshadow_id, expected_version,
        operation, body.model_dump(mode='json'))
    return await _run(db, apply_source_link(db, command,
        actor=getattr(request.state, 'auth_username', None)), write=True)


@router.post('/{foreshadow_id}/source', response_model=SourceDecisionResponse)
async def link_source(project_id: UUID, foreshadow_id: UUID, body: SourceLinkRequest, request: Request,
    db: AsyncSession = Depends(get_db), request_id: UUID = Header(..., alias='Idempotency-Key'),
    expected_version: str = Header(..., alias='If-Match', pattern='^[0-9a-f]{64}$')):
    return await _decide(project_id, foreshadow_id, body, request, db, request_id, expected_version, 'link')


@router.delete('/{foreshadow_id}/source', response_model=SourceDecisionResponse)
async def unlink_source(project_id: UUID, foreshadow_id: UUID, body: SourceDecisionRequest, request: Request,
    db: AsyncSession = Depends(get_db), request_id: UUID = Header(..., alias='Idempotency-Key'),
    expected_version: str = Header(..., alias='If-Match', pattern='^[0-9a-f]{64}$')):
    return await _decide(project_id, foreshadow_id, body, request, db, request_id, expected_version, 'unlink')
