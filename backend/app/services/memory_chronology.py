"""Resolve the persisted timeline used by chapter memory projections."""
import logging
from uuid import UUID

from sqlalchemy import select, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.project import Chapter, Volume

logger = logging.getLogger(__name__)


async def resolve_history_boundary(
    db: AsyncSession, project_id: str, volume_id: str, chapter_idx: int
) -> int:
    """Unknown, ambiguous or foreign targets cannot authorize history recall.

    Do not substitute volume-local numbering or a fresh chapter count: vectors
    carry the persisted Chapter.global_idx, which can differ after deletion.
    """
    try:
        project_id, volume_id = UUID(str(project_id)), UUID(str(volume_id))
        if type(chapter_idx) is not int or chapter_idx <= 0:
            return 0
        other_chapter, other_volume = aliased(Chapter), aliased(Volume)
        duplicate = exists(select(other_chapter.id).join(other_volume, other_chapter.volume_id == other_volume.id)
            .where(other_volume.project_id == project_id, other_chapter.global_idx == Chapter.global_idx,
                   other_chapter.id != Chapter.id)).correlate(Chapter)
        value = (await db.execute(
            select(Chapter.global_idx)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(
                Volume.project_id == project_id,
                Chapter.volume_id == volume_id,
                Chapter.chapter_idx == chapter_idx,
                ~duplicate,
            )
            .execution_options(autoflush=False)
        )).scalar_one_or_none()
        return value if type(value) is int and value > 0 else 0
    except Exception as exc:
        logger.warning("Chapter memory chronology unavailable: %s", type(exc).__name__)
        return 0
