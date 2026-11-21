"""Atomic first materialization of a volume; existing writing is never replaced."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from app.models.project import Project, Volume, Chapter, Outline
from app.services.budget_allocator import allocate_even, allocate_weighted, VOLUME_DEFAULT
from app.services.outline_contract import validate_volume_meta
from app.services.outline_generator import OutlineGenerator
from app.services.outline_consistency_gate import validate_outline_consistency
from app.services.outline_order import volume_matches, stub_payload, prepare_author_volume, bind_chapter_content


@dataclass(frozen=True)
class CreationSnapshot:
    project_id: UUID
    book_id: UUID
    volume_idx: int
    book_data: dict
    digest: str
    planned_volume_count: int | None


async def _read(db, project_id, book_id, idx, *, lock=False):
    if type(idx) is not int or idx < 1:
        raise ValueError('卷号必须为正整数')
    def query(model, *conditions):
        q = select(model).where(*conditions).execution_options(populate_existing=True)
        return q.with_for_update() if lock else q
    project = await db.scalar(query(Project, Project.id == project_id))
    if project is None or project.deleted_at is not None:
        raise ValueError('作品不存在')
    book = await db.scalar(query(Outline, Outline.id == book_id, Outline.project_id == project_id,
                                 Outline.level == 'book', Outline.is_confirmed == 1))
    if book is None:
        raise ValueError('请先确认本作品的全书大纲')
    rows = (await db.scalars(query(Volume, Volume.project_id == project_id, Volume.volume_idx == idx))).all()
    if len(rows) > 1:
        raise ValueError('同一卷号存在多个分卷，请先整理后再生成')
    volume = rows[0] if rows else None
    if volume and await db.scalar(query(Chapter, Chapter.volume_id == volume.id).limit(1)):
        raise ValueError('本卷已有章节，已保留；如需替换，请使用重新生成入口')
    outlines = (await db.scalars(query(Outline, Outline.project_id == project_id, Outline.level == 'volume'))).all()
    def matches(outline):
        content = outline.content_json or {}
        return volume_matches(content, volume.id if volume is not None else None, idx)

    matching = [outline for outline in outlines if matches(outline)]
    if matching:
        raise ValueError('本卷已有卷纲，已保留；请检查已有卷纲或使用重新生成入口')
    def values(row):
        return {c.key: getattr(row, c.key) for c in row.__table__.columns} if row else None
    data = {'project': values(project), 'book': values(book), 'volume': values(volume)}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return project, book, volume, digest


def _target(project, book_data, volume, idx, planned_count):
    plan = book_data.get('volume_plan')
    indices, weights = [], []
    if isinstance(plan, list) and plan:
        indices = [v.get('idx') if isinstance(v, dict) else None for v in plan]
        if not all(type(i) is int for i in indices) or sorted(indices) != list(range(1, len(plan) + 1)):
            raise ValueError('全书分卷计划卷号不完整，请先整理书纲')
        if idx not in indices:
            raise ValueError('请求卷号超出已确认书纲的分卷计划')
        weights = [v.get('est_chapters', 1) for v in plan]
        if any(type(w) is not int or w <= 0 for w in weights):
            raise ValueError('全书分卷计划章数无效')
    if volume is not None and int(volume.target_word_count) > 0 and int(volume.target_word_count) != VOLUME_DEFAULT:
        return int(volume.target_word_count)
    total = int(project.target_word_count or 0)
    if total <= 0:
        return VOLUME_DEFAULT
    if indices:
        return allocate_weighted(total, weights)[indices.index(idx)]
    if type(planned_count) is not int or planned_count < idx:
        raise ValueError('缺少有效分卷计划，请填写总卷数或补全书纲')
    return allocate_even(total, planned_count)[idx - 1]


async def snapshot_creation(db, project_id, book_id, idx, planned_count=None):
    pid, bid = UUID(str(project_id)), UUID(str(book_id))
    project, book, volume, digest = await _read(db, pid, bid, idx)
    book_data = deepcopy(book.content_json or {})
    _target(project, book_data, volume, idx, planned_count)  # Fail before model work.
    return CreationSnapshot(pid, bid, idx, book_data, digest, planned_count)


async def materialize_volume(db, snapshot, parsed):
    """Caller commits the outline and materialized rows together."""
    validate_volume_meta(parsed, snapshot.volume_idx)
    OutlineGenerator._validate_volume_batch(parsed.get('chapter_summaries'), 1, parsed['chapter_count'])
    validate_outline_consistency(parsed, level='volume')
    from app.services.chapter_order import lock_chapter_structure, reindex_chapter_structure
    await lock_chapter_structure(db, snapshot.project_id, writer_owned=True)
    project, book, volume, digest = await _read(db, snapshot.project_id, snapshot.book_id, snapshot.volume_idx, lock=True)
    if digest != snapshot.digest:
        raise ValueError('生成期间作品或分卷资料已变化，请刷新后重试')
    target = _target(project, snapshot.book_data, volume, snapshot.volume_idx, snapshot.planned_volume_count)
    if volume is None:
        volume = Volume(project_id=project.id, volume_idx=snapshot.volume_idx,
                        title=parsed['title'], summary=parsed['core_conflict'], target_word_count=target)
        db.add(volume)
        await db.flush()
    else:
        volume.title, volume.summary = parsed['title'], parsed['core_conflict']
        volume.target_word_count = target
    content = {**deepcopy(parsed), 'volume_id': str(volume.id),
               'chapter_summaries': [stub_payload(item) for item in parsed['chapter_summaries']]}
    outline = Outline(project_id=project.id, parent_id=book.id, level='volume', content_json=content)
    db.add(outline)
    created = []
    for item, budget in zip(content['chapter_summaries'], allocate_even(target, parsed['chapter_count'])):
        chapter = Chapter(volume_id=volume.id, chapter_idx=item['chapter_idx'], title=item['title'],
                          outline_json=deepcopy(item), target_word_count=budget, global_idx=0)
        created.append(chapter)
        db.add(chapter)
    await db.flush()
    outline.content_json = prepare_author_volume({}, content, volume, created)
    for chapter in created:
        chapter.outline_json = bind_chapter_content(chapter.outline_json, chapter)
    await db.flush()
    await reindex_chapter_structure(db, snapshot.project_id)
    return outline
