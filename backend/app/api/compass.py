"""Narrative compass endpoints (C4 / F3, ainovel compass)."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Chapter, Foreshadow, Outline, Project, Volume
from app.services import compass_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/compass",
    tags=["compass"],
)


class CompassUpdate(BaseModel):
    ending_direction: str | None = None
    open_threads: list[dict] | None = None
    estimated_scale: dict | None = None


@router.get("")
async def get_compass(project_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Return the project's narrative compass (empty fields if not initialized)."""
    return await compass_service.load_compass(db, project_id)


@router.put("")
async def put_compass(
    project_id: UUID, body: CompassUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    """Manually set/correct compass fields (partial update)."""
    compass = await compass_service.load_compass(db, project_id)
    if body.ending_direction is not None:
        compass["ending_direction"] = body.ending_direction
    if body.open_threads is not None:
        compass["open_threads"] = body.open_threads
    if body.estimated_scale is not None:
        compass["estimated_scale"] = body.estimated_scale
    await compass_service.save_compass(db, project_id, compass)
    return compass


@router.post("/refresh")
async def refresh_compass(project_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """(Re)initialize the compass from the current book outline.

    Reliable manual path independent of the auto-population hooks.
    """
    project = await db.get(Project, project_id)
    target_wc = getattr(project, "target_word_count", None) if project else None
    book = (
        await db.execute(
            select(Outline.content_json)
            .where(Outline.project_id == str(project_id), Outline.level == "book")
            .order_by(Outline.created_at.desc())
            .limit(1)
        )
    ).first()
    content_json = book[0] if book else {}
    return await compass_service.initialize_from_book_outline(
        db, project_id, content_json, target_word_count=target_wc
    )


@router.get("/completion-readiness")
async def completion_readiness(
    project_id: UUID, db: AsyncSession = Depends(get_db)
) -> dict:
    """Run the six-point completion checklist for the project."""
    compass = await compass_service.load_compass(db, project_id)

    written = int(
        (
            await db.execute(
                select(func.count(Chapter.id))
                .select_from(Chapter)
                .join(Volume, Chapter.volume_id == Volume.id)
                .where(
                    Volume.project_id == str(project_id),
                    Chapter.content_text.isnot(None),
                    Chapter.content_text != "",
                )
            )
        ).scalar()
        or 0
    )
    unresolved = int(
        (
            await db.execute(
                select(func.count(Foreshadow.id)).where(
                    Foreshadow.project_id == str(project_id),
                    Foreshadow.status != "resolved",
                )
            )
        ).scalar()
        or 0
    )
    # Recent chapter summaries for the steady-state heuristic.
    summary_rows = (
        await db.execute(
            select(Chapter.summary)
            .select_from(Chapter)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == str(project_id), Chapter.summary.isnot(None))
            .order_by(Volume.volume_idx.desc(), Chapter.chapter_idx.desc())
            .limit(5)
        )
    ).all()
    recent = [s for (s,) in summary_rows if s]

    return compass_service.assess_completion_readiness(
        compass, written, unresolved, recent_summaries=recent
    )
