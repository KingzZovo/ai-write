"""One committed structural mutation per request, including both sides of a move."""
import asyncio
from contextlib import asynccontextmanager
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.chapter_order import (
    ChapterOrderConflict, ChapterOrderUnavailable, lock_chapter_structure, swap_adjacent_chapters,
)
from app.services.entity_materialization_lock import MaterializationBusy

router = APIRouter()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def structure_write(db, project_id):
    try:
        async with asyncio.timeout(30):
            await lock_chapter_structure(db, project_id)
            yield
            await db.commit()
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except HTTPException:
        await db.rollback()
        raise
    except ChapterOrderUnavailable as exc:
        await db.rollback()
        raise HTTPException(404, detail={'code': 'chapter_order_unavailable', 'message': str(exc)}) from None
    except (ChapterOrderConflict, MaterializationBusy) as exc:
        await db.rollback()
        message = str(exc) if isinstance(exc, ChapterOrderConflict) else '作品资料正在同步，请稍后再操作。'
        raise HTTPException(409, detail={'code': 'chapter_order_conflict', 'message': message}) from None
    except Exception as exc:
        await db.rollback()
        logger.warning('Chapter structure save not confirmed (%s: %s)', type(exc).__name__, exc, exc_info=True)
        raise HTTPException(503, detail={'code': 'chapter_order_unconfirmed',
            'message': '卷章调整未取得保存确认，请先刷新核对实际顺序，再决定下一步。'}) from None


class ChapterMoveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    neighbor_id: UUID
    expected_version: str = Field(..., strict=True, pattern='^[0-9a-f]{64}$')
    expected_neighbor_version: str = Field(..., strict=True, pattern='^[0-9a-f]{64}$')


class ChapterMoveResponse(BaseModel):
    chapter_id: UUID
    neighbor_id: UUID
    chapter_idx: int
    neighbor_chapter_idx: int


@router.post('/{chapter_id}/move', response_model=ChapterMoveResponse)
async def move_chapter(project_id: UUID, chapter_id: UUID, body: ChapterMoveRequest, db: AsyncSession = Depends(get_db)):
    async with structure_write(db, project_id):
        result = await swap_adjacent_chapters(db, project_id, chapter_id, body.neighbor_id,
            body.expected_version, body.expected_neighbor_version)
        return result
