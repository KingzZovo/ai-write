"""v0.6 reference-book decompile API.

- POST /api/reference-books/{id}/reprocess: enqueue decompile pipeline
- POST /api/reference-books/{id}/retry-missing: re-run style/beat branches
    only for slices whose cards are still missing (partial-failure recovery).
- GET  /api/reference-books/{id}/slices: list semantic slices
- GET  /api/reference-books/{id}/style-profiles: list style profile cards
- GET  /api/reference-books/{id}/beat-sheets: list beat sheet cards
- GET  /api/reference-books/{id}/decompile-status: progress summary

Consolidation layer (book dossier):
- POST /api/decompile/{id}/consolidate: aggregate micro-cards into one dossier
- GET  /api/decompile/{id}/dossier: return dossier + status

The router carries no prefix (paths are spelled out per route) so the two URL
spaces /api/reference-books and /api/decompile can share this one router
without touching main.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import get_db
from app.models.decompile import BeatSheetCard, ReferenceBookSlice, StyleProfileCard
from app.models.project import ReferenceBook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["decompile"])

_PREFIX = "/api/reference-books"

# Keep strong references to inline background consolidations so they are not
# garbage-collected mid-run (celery-unavailable fallback path).
_bg_tasks: set[asyncio.Task] = set()


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
    style_covered_slices: int = 0
    beat_covered_slices: int = 0
    missing_style: int = 0
    missing_beat: int = 0
    retry_attempt: int = 0
    retry_max_attempts: int = 0
    last_run_at: str | None = None
    error_message: str | None = None


@router.post(_PREFIX + "/{book_id}/reprocess", response_model=ReprocessResponse)
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


@router.post(_PREFIX + "/{book_id}/retry-missing", response_model=ReprocessResponse)
async def retry_missing(
    book_id: UUID, db: AsyncSession = Depends(get_db)
) -> ReprocessResponse:
    """Manually re-run style/beat branches for slices whose cards are missing.

    Designed to be called from the frontend when a book is in ``partial``
    state. Does not wipe any existing slices or cards.
    """
    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="reference book not found")

    # The current attempt counter (if any) lives in metadata_json. Bump by 1
    # so manual retries follow the same numbering scheme as automatic ones.
    meta = book.metadata_json or {}
    prev_attempt = int((meta.get("decompile_retry") or {}).get("attempt") or 0)
    next_attempt = prev_attempt + 1

    try:
        from app.tasks import celery_app  # noqa: WPS433

        async_result = celery_app.send_task(
            "retry_reference_book_missing_branches",
            args=[str(book_id), next_attempt],
        )
        return ReprocessResponse(status="queued", task_id=async_result.id)
    except Exception as exc:
        logger.warning("celery enqueue failed for retry, running inline: %s", exc)
        from app.services.reference_ingestor import retry_missing_branches

        summary = await retry_missing_branches(
            book_id=str(book_id), attempt=next_attempt, db=db
        )
        return ReprocessResponse(status=summary.get("status", "unknown"))


@router.get(_PREFIX + "/{book_id}/slices", response_model=list[SliceOut])
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


@router.get(_PREFIX + "/{book_id}/style-profiles", response_model=list[StyleProfileOut])
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


@router.get(_PREFIX + "/{book_id}/beat-sheets", response_model=list[BeatSheetOut])
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


@router.get(_PREFIX + "/{book_id}/decompile-status", response_model=DecompileStatus)
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
    style_covered = (
        await db.scalar(
            select(func.count(func.distinct(StyleProfileCard.slice_id))).where(
                StyleProfileCard.book_id == str(book_id)
            )
        )
    ) or 0
    beat_covered = (
        await db.scalar(
            select(func.count(func.distinct(BeatSheetCard.slice_id))).where(
                BeatSheetCard.book_id == str(book_id)
            )
        )
    ) or 0
    retry_meta = (book.metadata_json or {}).get("decompile_retry") or {}
    return DecompileStatus(
        book_id=book_id,
        book_status=book.status or "unknown",
        slice_count=slice_count,
        style_card_count=style_count,
        beat_card_count=beat_count,
        style_covered_slices=int(style_covered),
        beat_covered_slices=int(beat_covered),
        missing_style=max(int(slice_count) - int(style_covered), 0),
        missing_beat=max(int(slice_count) - int(beat_covered), 0),
        retry_attempt=int(retry_meta.get("attempt") or 0),
        retry_max_attempts=int(retry_meta.get("max_attempts") or 0),
        last_run_at=retry_meta.get("last_run_at"),
        error_message=book.error_message,
    )


# =========================================================================
# Consolidation layer — book dossier
# =========================================================================

class ConsolidateResponse(BaseModel):
    status: str
    task_id: str | None = None


class DossierResponse(BaseModel):
    book_id: UUID
    status: dict | None = None
    dossier: dict | None = None


@router.post(
    "/api/decompile/{book_id}/consolidate",
    response_model=ConsolidateResponse,
    status_code=202,
)
async def consolidate(
    book_id: UUID, db: AsyncSession = Depends(get_db)
) -> ConsolidateResponse:
    """Aggregate the book's micro-cards into one dossier (async, idempotent).

    Marks ``metadata_json['dossier_status']`` as queued, then fires the
    celery task; if the broker is unreachable, falls back to an in-process
    background asyncio task. Re-runs overwrite the stored dossier.
    """
    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="reference book not found")

    meta = dict(book.metadata_json or {})
    meta["dossier_status"] = {
        "state": "queued",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    book.metadata_json = meta
    flag_modified(book, "metadata_json")
    await db.commit()

    try:
        from app.tasks import celery_app  # noqa: WPS433

        async_result = celery_app.send_task(
            "tasks.consolidate_reference_book",
            args=[str(book_id)],
        )
        return ConsolidateResponse(status="queued", task_id=async_result.id)
    except Exception as exc:
        logger.warning(
            "celery enqueue failed for consolidate, running in background: %s", exc
        )
        from app.services.book_dossier import build_dossier

        task = asyncio.create_task(build_dossier(str(book_id)))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return ConsolidateResponse(status="started")


@router.get("/api/decompile/{book_id}/dossier", response_model=DossierResponse)
async def get_dossier(
    book_id: UUID, db: AsyncSession = Depends(get_db)
) -> DossierResponse:
    """Return the consolidated dossier and its status marker."""
    book = await db.get(ReferenceBook, str(book_id))
    if book is None:
        raise HTTPException(status_code=404, detail="reference book not found")
    meta = book.metadata_json or {}
    return DossierResponse(
        book_id=book_id,
        status=meta.get("dossier_status"),
        dossier=meta.get("dossier"),
    )
