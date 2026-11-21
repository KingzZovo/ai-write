"""Maintain saved book order at structural writes, never by guessing on reads."""
from collections.abc import Mapping
from datetime import timedelta, timezone
from uuid import UUID

from sqlalchemy import case, select, update

from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Project, Volume, _utcnow
from app.services.entity_materialization_lock import MaterializationBusy, require_materialization_lock
from app.services.entity_sources import digest
from app.services.production.control import ACTIVE_STATES
from app.services.production.locking import try_command_lock


class ChapterOrderConflict(ValueError):
    pass


class ChapterOrderUnavailable(LookupError):
    pass


def chapter_order_version(chapter):
    def get(key):
        return chapter[key] if isinstance(chapter, Mapping) else getattr(chapter, key)
    stamp = get('updated_at')
    if stamp is not None:
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    return digest(dict(id=str(get('id')), volume_id=str(get('volume_id')),
        chapter_idx=get('chapter_idx'), global_idx=get('global_idx'),
        updated_at=stamp.isoformat(timespec='microseconds') if stamp is not None else None))


async def lock_chapter_structure(db, project_id, *, writer_owned=False):
    """Order: production mutex -> materialization mutex -> project -> children.

    writer_owned is only for volume generation already inside guarded_stream;
    that lease lives on another connection and must not be acquired again.
    This function does not commit or call a model/graph/broker.
    """
    pid = UUID(str(project_id))
    if not writer_owned:
        if not await try_command_lock(db, pid):
            raise ChapterOrderConflict('本书正在生成或调整章节，请稍后再操作。')
        active = await db.scalar(select(PipelineRun.id).where(
            PipelineRun.project_id == pid, PipelineRun.state.in_(ACTIVE_STATES),
        ).limit(1).execution_options(autoflush=False))
        if active is not None:
            raise ChapterOrderConflict('请先暂停整书生成，再调整卷章顺序。')
    try:
        await require_materialization_lock(db, pid)
    except MaterializationBusy:
        raise ChapterOrderConflict('作品资料正在同步，请稍后再操作。') from None
    project = await db.scalar(select(Project.id).where(Project.id == pid, Project.deleted_at.is_(None))
        .with_for_update().execution_options(autoflush=False))
    if project is None:
        raise ChapterOrderUnavailable('作品不存在或已删除。')


async def require_free_index(db, model, parent_id, index, *, excluding=None):
    if type(index) is not int or not 0 < index <= 2147483647:
        raise ChapterOrderConflict('卷号和章号必须是有效的正整数。')
    column, parent = (Volume.volume_idx, Volume.project_id) if model is Volume else (Chapter.chapter_idx, Chapter.volume_id)
    query = select(model.id).where(parent == UUID(str(parent_id)), column == index)
    if excluding is not None:
        query = query.where(model.id != UUID(str(excluding)))
    if await db.scalar(query.limit(1).execution_options(autoflush=False)) is not None:
        raise ChapterOrderConflict('目标编号已被占用，请刷新核对；移动章节请使用上移或下移。')


async def read_chapter_structure(db, project_id):
    """Only metadata is read, even for a book with millions of prose characters."""
    pid = UUID(str(project_id))
    volumes = list((await db.execute(select(Volume.id, Volume.volume_idx).where(Volume.project_id == pid)
        .order_by(Volume.id).with_for_update().execution_options(autoflush=False))).mappings().all())
    chapters = list((await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Chapter.updated_at).join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == pid).order_by(Chapter.id).with_for_update(of=Chapter)
        .execution_options(autoflush=False))).mappings().all())
    return volumes, chapters


def ordered_chapters(volumes, chapters):
    volume_indices = {row['id']: row['volume_idx'] for row in volumes}
    pairs = [(row['volume_id'], row['chapter_idx']) for row in chapters]
    indices = list(volume_indices.values()) + [row['chapter_idx'] for row in chapters]
    if (any(type(idx) is not int or not 0 < idx <= 2147483647 for idx in indices)
            or len(set(volume_indices.values())) != len(volume_indices)
            or len(set(pairs)) != len(pairs)):
        raise ChapterOrderConflict('现有卷号或章号重复或无效，请先明确整理；未猜测原有顺序。')
    return sorted(chapters, key=lambda row: (volume_indices[row['volume_id']], row['chapter_idx']))


def next_order_timestamp(chapters):
    """Advance versions even if the clock stalls or labels later move back."""
    now = _utcnow()
    for row in chapters:
        previous = row['updated_at']
        if previous is not None:
            previous = previous.replace(tzinfo=timezone.utc) if previous.tzinfo is None else previous.astimezone(timezone.utc)
            now = max(now, previous + timedelta(microseconds=1))
    return now


async def reindex_chapter_structure(db, project_id, *, previous_volume_indices=None):
    """Caller holds the project structure lock and commits the mutation with this.

    Existing source-change/invalidation triggers observe the same transaction.
    Historical facts and original source receipts are never renumbered here.
    """
    volumes, chapters = await read_chapter_structure(db, project_id)
    ordered = ordered_chapters(volumes, chapters)
    changes = [(row['id'], index) for index, row in enumerate(ordered, 1) if row['global_idx'] != index]
    stamp = next_order_timestamp(chapters)
    for start in range(0, len(changes), 200):
        batch = dict(changes[start:start + 200])
        await db.execute(update(Chapter).where(Chapter.id.in_(batch), Chapter.volume_id.in_(
            select(Volume.id).where(Volume.project_id == UUID(str(project_id)))))
            .values(global_idx=case(batch, value=Chapter.id), updated_at=stamp)
            .execution_options(synchronize_session=False, autoflush=False))
    from app.services.outline_order import maintain_outline_order
    await maintain_outline_order(db, project_id, previous_volume_indices=previous_volume_indices)
    return len(changes)


async def swap_adjacent_chapters(db, project_id, chapter_id, neighbor_id, expected_version, expected_neighbor_version):
    volumes, chapters = await read_chapter_structure(db, project_id)
    ordered = ordered_chapters(volumes, chapters)
    by_id = {str(row['id']): row for row in ordered}
    chapter, neighbor = by_id.get(str(chapter_id)), by_id.get(str(neighbor_id))
    if chapter is None or neighbor is None:
        raise ChapterOrderUnavailable('章节不存在或不属于本书。')
    if (chapter['id'] == neighbor['id'] or chapter['volume_id'] != neighbor['volume_id']
            or abs(ordered.index(chapter) - ordered.index(neighbor)) != 1):
        raise ChapterOrderConflict('只能交换本卷相邻章节，请刷新后核对。')
    if (chapter_order_version(chapter) != expected_version
            or chapter_order_version(neighbor) != expected_neighbor_version):
        raise ChapterOrderConflict('章节或顺序已变化，请刷新核对后再移动。')
    indices = {chapter['id']: neighbor['chapter_idx'], neighbor['id']: chapter['chapter_idx']}
    await db.execute(update(Chapter).where(Chapter.id.in_(indices)).values(
        chapter_idx=case(indices, value=Chapter.id), updated_at=next_order_timestamp(chapters))
        .execution_options(synchronize_session=False, autoflush=False))
    await reindex_chapter_structure(db, project_id)
    return dict(chapter_id=chapter['id'], neighbor_id=neighbor['id'],
        chapter_idx=neighbor['chapter_idx'], neighbor_chapter_idx=chapter['chapter_idx'])
