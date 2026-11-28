"""Source-checked author saves with a receipt issued after commit."""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, Project, Volume
from app.services.author_manuscripts import prepare_author_manuscript
from app.services.chapter_content import ChapterContentError
from app.services.chapter_order import ChapterOrderConflict, ChapterOrderUnavailable

router = APIRouter()
logger = logging.getLogger(__name__)


class ManuscriptSource(BaseModel):
    project_id: str
    chapter_id: str
    title: str
    content_text: str
    word_count: int
    status: str


class ManuscriptSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content_text: str
    expected_content_text: str | None = None
    force: bool = False
    # An author can retain an unconfirmed generated fragment as a draft, or
    # explicitly keep a rejected candidate for review. This is not approval.
    status: Literal['draft', 'needs_review'] | None = None


class ManuscriptReceipt(BaseModel):
    status: Literal['saved'] = 'saved'
    manuscript: ManuscriptSource
    changed: bool
    backup_version_id: str | None = None


async def _chapter(db, project_id, chapter_id, *, lock=False):
    query = select(Chapter).join(Volume, Chapter.volume_id == Volume.id).join(Project, Volume.project_id == Project.id).where(
        Chapter.id == chapter_id, Volume.project_id == project_id, Project.deleted_at.is_(None),
    ).execution_options(populate_existing=True, autoflush=False)
    if lock:
        query = query.with_for_update(of=Chapter)
    chapter = await db.scalar(query)
    if chapter is None:
        raise HTTPException(404, detail={'code': 'chapter_manuscript_unavailable', 'message': '章节不存在或作品已删除。'})
    return chapter


def manuscript_source(project_id, chapter):
    content = chapter.content_text or ''
    return ManuscriptSource(project_id=str(project_id), chapter_id=str(chapter.id), title=chapter.title or '',
                            content_text=content, word_count=len(content), status=chapter.status)


@router.get('/{chapter_id}/content', response_model=ManuscriptSource)
async def read_manuscript(project_id: UUID, chapter_id: UUID, db: AsyncSession = Depends(get_db)):
    return manuscript_source(project_id, await _chapter(db, project_id, chapter_id))


@asynccontextmanager
async def author_manuscript_write(db):
    """Only exit successfully after the complete author mutation is committed."""
    try:
        async with asyncio.timeout(30):
            yield
            await db.commit()
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except HTTPException:
        await db.rollback()
        raise
    except ChapterContentError as exc:
        await db.rollback()
        status = 428 if exc.code == 'chapter_content_precondition_required' else 409 if exc.code == 'chapter_edit_conflict' else 400
        raise HTTPException(status, detail=exc.detail) from None
    except ChapterOrderUnavailable as exc:
        await db.rollback()
        raise HTTPException(404, detail={'code': 'chapter_manuscript_unavailable', 'message': str(exc)}) from None
    except ChapterOrderConflict as exc:
        await db.rollback()
        raise HTTPException(409, detail={'code': 'chapter_manuscript_busy', 'message': str(exc)}) from None
    except Exception as exc:
        await db.rollback()
        logger.warning('Chapter manuscript save not confirmed (%s)', type(exc).__name__)
        raise HTTPException(503, detail={'code': 'chapter_save_unconfirmed',
            'message': '未取得正文保存确认，请保留编辑区稿件并重新读取保存稿核对。'}) from None


@router.put('/{chapter_id}/content', response_model=ManuscriptReceipt)
async def save_manuscript(project_id: UUID, chapter_id: UUID, body: ManuscriptSave, db: AsyncSession = Depends(get_db)):
    async with author_manuscript_write(db):
        change = await prepare_author_manuscript(db, project_id, chapter_id, text=body.content_text,
                                                expected=body.expected_content_text, force=body.force)
        chapter = change.chapter
        if change.changed:
            chapter.content_text = change.text
        chapter.word_count = len(change.text)
        if body.status is not None:
            chapter.status = body.status
        if change.changed:
            # 正文变了 → 旧认知记录失效。不重入队,这一章就成为其后所有生成的硬阻塞。
            from app.services.cognition_extraction import ensure_chapter_cognition
            await ensure_chapter_cognition(db, project_id, chapter_id)
        await db.flush()
        response = ManuscriptReceipt(manuscript=manuscript_source(project_id, chapter), changed=change.changed,
                                     backup_version_id=change.backup_version_id)
        chapter_idx = chapter.chapter_idx
    if change.changed:
        from app.services.entity_dispatch import dispatch_entity_extraction
        dispatch_entity_extraction(project_id=project_id, chapter_id=chapter_id, chapter_idx=chapter_idx,
                                   caller='api.chapter_manuscripts.save')
    return response
