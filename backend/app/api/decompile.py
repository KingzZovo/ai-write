"""v0.6 reference-book decompile API.

- POST /api/reference-books/{id}/reprocess: enqueue decompile pipeline
- GET  /api/reference-books/{id}/slices: list semantic slices
- GET  /api/reference-books/{id}/style-profiles: list style profile cards
- GET  /api/reference-books/{id}/beat-sheets: list beat sheet cards
- GET  /api/reference-books/{id}/decompile-status: progress summary
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
from app.models.project import ReferenceBook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reference-books", tags=["decompile"])


class SliceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slice_type: str
    sequence_id: int
    chapter_idx: int | None
    token_count: int


class StyleProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slice_id: UUID
    profile_json: dict
    qdrant_point_id: str | None


class BeatSheetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slice_id: UUID
    beat_json: dict
    qdrant_point_id: str | None


class ReprocessResponse(BaseModel):
    status: str
    task_id: str | None = None


class DecompileStatus(BaseModel):
    book_id: UUID
    book_status: str
    slice_count: int
    style_card_count: int
    beat_card_count: int


@router.post("/{book_id}/reprocess", response_model=ReprocessResponse)
async def reprocess(book_id: UUID, db: AsyncSession = Depends(get_db)) -> ReprocessResponse:
    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="reference book not found")

    # Try Celery first; if broker unreachable, fall back to synchronous run.
    try:
        from app.tasks import celery_app  # noqa: WPS433

        async_result = celery_app.send_task(
            "reprocess_reference_book",
            args=[str(book_id)],
        )
        return ReprocessResponse(status="queued", task_id=async_result.id)
    except Exception as exc:
        logger.warning("celery enqueue failed, running inline: %s", exc)
        from app.services.reference_ingestor import reprocess_reference_book

        summary = await reprocess_reference_book(book_id=str(book_id), db=db)
        return ReprocessResponse(status=summary.get("status", "unknown"))


@router.get("/{book_id}/slices", response_model=list[SliceOut])
async def list_slices(
    book_id: UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
) -> list[SliceOut]:
    rows = await db.execute(
        select(ReferenceBookSlice)
        .where(ReferenceBookSlice.book_id == str(book_id))
        .order_by(ReferenceBookSlice.sequence_id.asc())
        .limit(limit)
    )
    return [SliceOut.model_validate(s) for s in rows.scalars().all()]


@router.get("/{book_id}/style-profiles", response_model=list[StyleProfileOut])
async def list_style_profiles(
    book_id: UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
) -> list[StyleProfileOut]:
    rows = await db.execute(
        select(StyleProfileCard)
        .where(StyleProfileCard.book_id == str(book_id))
        .order_by(StyleProfileCard.created_at.asc())
        .limit(limit)
    )
    return [StyleProfileOut.model_validate(r) for r in rows.scalars().all()]


@router.get("/{book_id}/beat-sheets", response_model=list[BeatSheetOut])
async def list_beat_sheets(
    book_id: UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
) -> list[BeatSheetOut]:
    rows = await db.execute(
        select(BeatSheetCard)
        .where(BeatSheetCard.book_id == str(book_id))
        .order_by(BeatSheetCard.created_at.asc())
        .limit(limit)
    )
    return [BeatSheetOut.model_validate(r) for r in rows.scalars().all()]


@router.get("/{book_id}/decompile-status", response_model=DecompileStatus)
async def decompile_status(
    book_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> DecompileStatus:
    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="reference book not found")
    slice_count = (
        await db.scalar(
            select(func.count(ReferenceBookSlice.id)).where(
                ReferenceBookSlice.book_id == str(book_id)
            )
        )
    ) or 0
    style_count = (
        await db.scalar(
            select(func.count(StyleProfileCard.id)).where(
                StyleProfileCard.book_id == str(book_id)
            )
        )
    ) or 0
    beat_count = (
        await db.scalar(
            select(func.count(BeatSheetCard.id)).where(
                BeatSheetCard.book_id == str(book_id)
            )
        )
    ) or 0
    return DecompileStatus(
        book_id=book_id,
        book_status=book.status or "unknown",
        slice_count=slice_count,
        style_card_count=style_count,
        beat_card_count=beat_count,
    )
