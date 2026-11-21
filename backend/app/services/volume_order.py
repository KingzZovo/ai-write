"""Swap adjacent saved volumes in the caller's single structure transaction."""
from uuid import UUID

from sqlalchemy import case, update

from app.models.project import Volume
from app.services.chapter_order import (
    ChapterOrderConflict, ChapterOrderUnavailable, ordered_chapters,
    read_chapter_structure, reindex_chapter_structure,
)


async def swap_adjacent_volumes(db, project_id, volume_id, neighbor_id, expected_idx, expected_neighbor_idx):
    volumes, chapters = await read_chapter_structure(db, project_id)
    ordered_chapters(volumes, chapters)  # Reject ambiguous historical numbering.
    ordered = sorted(volumes, key=lambda row: row['volume_idx'])
    by_id = {str(row['id']): row for row in ordered}
    volume, neighbor = by_id.get(str(volume_id)), by_id.get(str(neighbor_id))
    if volume is None or neighbor is None:
        raise ChapterOrderUnavailable('分卷不存在或不属于本书。')
    if volume['id'] == neighbor['id'] or abs(ordered.index(volume) - ordered.index(neighbor)) != 1:
        raise ChapterOrderConflict('只能移动到相邻分卷的位置，请重新核对目录。')
    if volume['volume_idx'] != expected_idx or neighbor['volume_idx'] != expected_neighbor_idx:
        raise ChapterOrderConflict('分卷顺序已变化，请重新核对后再移动。')
    indices = {volume['id']: neighbor['volume_idx'], neighbor['id']: volume['volume_idx']}
    await db.execute(update(Volume).where(Volume.project_id == UUID(str(project_id)), Volume.id.in_(indices))
        .values(volume_idx=case(indices, value=Volume.id))
        .execution_options(synchronize_session=False, autoflush=False))
    await reindex_chapter_structure(db, project_id, previous_volume_indices={
        str(volume['id']): volume['volume_idx'], str(neighbor['id']): neighbor['volume_idx']})
    return {'volume_id': str(volume['id']), 'neighbor_id': str(neighbor['id']),
            'volume_idx': neighbor['volume_idx'], 'neighbor_volume_idx': volume['volume_idx']}
