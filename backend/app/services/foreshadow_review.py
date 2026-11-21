"""Bind read-only payoff suggestions to a persisted chapter snapshot."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Chapter, Volume
from app.services.context_errors import RequiredContextUnavailable
from app.services.foreshadow_manager import ForeshadowManager, ResolutionSuggestion


@dataclass(frozen=True)
class ScopedResolutionReview:
    project_id: str
    volume_id: str
    chapter_id: str
    chapter_idx: int
    global_idx: int
    source_sha256: str
    source_updated_at: datetime | None
    source_status: str
    suggestions: tuple[ResolutionSuggestion, ...]


async def review_scoped_chapter(
    *, db: AsyncSession, manager: ForeshadowManager, project_id: str,
    volume_id: str, chapter_idx: int, expected_text: str,
) -> ScopedResolutionReview:
    """Revalidate source after inference; do not hold a row lock over model IO.

    This is snapshot provenance, not a promise that a later adoption is safe.
    A future persistence/adoption transaction must revalidate the snapshot.
    """
    query = (
        select(Chapter.id, Chapter.global_idx, Chapter.content_text, Chapter.updated_at, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == UUID(str(project_id)), Chapter.volume_id == UUID(str(volume_id)),
               Chapter.chapter_idx == chapter_idx)
        .limit(2).execution_options(autoflush=False)
    )
    # Select scalar columns so the ORM identity map cannot supply an old body.
    rows = (await db.execute(query)).all()
    if len(rows) != 1:
        raise RequiredContextUnavailable('foreshadow_review_source_missing_or_ambiguous')
    source = tuple(rows[0])
    source_id, global_idx, text, updated_at, status = source
    if type(global_idx) is not int or global_idx <= 0:
        raise RequiredContextUnavailable('foreshadow_review_chronology_unavailable')
    if not isinstance(text, str) or not text.strip() or text != expected_text:
        raise RequiredContextUnavailable('foreshadow_review_source_changed')

    suggestions = await manager.check_resolution(project_id, text, chapter_idx=global_idx)

    current_rows = (await db.execute(query.where(Chapter.id == source_id))).all()
    if len(current_rows) != 1 or tuple(current_rows[0]) != source:
        raise RequiredContextUnavailable('foreshadow_review_source_changed')
    return ScopedResolutionReview(
        project_id=project_id, volume_id=volume_id, chapter_id=str(source_id),
        chapter_idx=chapter_idx, global_idx=global_idx,
        source_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
        source_updated_at=updated_at, source_status=status,
        suggestions=tuple(suggestions),
    )
