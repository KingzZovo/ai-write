"""Stable chapter identities and explicit bounds for foreshadow references."""
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.models.project import Chapter, Volume


def positive_index(value):
    return type(value) is int and 0 < value <= 2147483647


@dataclass(frozen=True)
class ForeshadowBoundary:
    volume_idx: int
    chapter_idx: int | None = None
    global_idx: int | None = None

    def includes(self, position, *, planned=False):
        if self.chapter_idx is None:
            return position['volume_idx'] < self.volume_idx
        current = (self.volume_idx, self.chapter_idx)
        previous = (position['volume_idx'], position['chapter_idx'])
        # Check both axes. A reordered volume with old global indices must
        # never let a physically later chapter masquerade as prior prose.
        if planned:
            return previous <= current and position['global_idx'] <= self.global_idx
        return previous < current and position['global_idx'] < self.global_idx


@dataclass
class ForeshadowPositions:
    volumes: dict
    chapters: dict
    volume_counts: Counter
    local_counts: Counter
    global_counts: Counter
    local_ids: dict
    global_ids: dict

    def volume(self, volume_id):
        row = self.volumes.get(str(volume_id))
        if row and positive_index(row['volume_idx']) and self.volume_counts[row['volume_idx']] == 1:
            return row
        return None

    def chapter(self, chapter_id):
        row = self.chapters.get(str(chapter_id))
        if (not row or not self.volume(row['volume_id']) or not positive_index(row['chapter_idx'])
                or not positive_index(row['global_idx'])
                or self.local_counts[(str(row['volume_id']), row['chapter_idx'])] != 1
                or self.global_counts[row['global_idx']] != 1):
            return None
        return {**row, 'volume_idx': self.volumes[str(row['volume_id'])]['volume_idx']}

    def at_local(self, volume_id, chapter_idx):
        chapter_id = self.local_ids.get((str(volume_id), chapter_idx))
        return self.chapter(chapter_id) if chapter_id is not None else None

    def boundary(self, global_idx=None, *, volume_idx=None):
        if global_idx is not None:
            if not positive_index(global_idx) or self.global_counts[global_idx] != 1:
                raise ValueError('foreshadow_chapter_position_unavailable')
            position = self.chapter(self.global_ids[global_idx])
            if position is None:
                raise ValueError('foreshadow_chapter_position_unavailable')
            return ForeshadowBoundary(position['volume_idx'], position['chapter_idx'], global_idx)
        # A new volume need not exist yet, but its intended index must be
        # explicit. None must never mean "read the entire book as history".
        if not positive_index(volume_idx) or self.volume_counts[volume_idx] > 1:
            raise ValueError('foreshadow_volume_position_unavailable')
        return ForeshadowBoundary(volume_idx)


async def load_foreshadow_positions(db, project_id):
    pid = UUID(str(project_id))
    volumes = (await db.execute(select(Volume.id, Volume.volume_idx, Volume.title)
        .where(Volume.project_id == pid).execution_options(autoflush=False))).mappings().all()
    chapters = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Chapter.status, Chapter.title).join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == pid).execution_options(autoflush=False))).mappings().all()
    return ForeshadowPositions(
        {str(v['id']): dict(v) for v in volumes}, {str(c['id']): dict(c) for c in chapters},
        Counter(v['volume_idx'] for v in volumes),
        Counter((str(c['volume_id']), c['chapter_idx']) for c in chapters),
        Counter(c['global_idx'] for c in chapters),
        {(str(c['volume_id']), c['chapter_idx']): c['id'] for c in chapters},
        {c['global_idx']: c['id'] for c in chapters},
    )
