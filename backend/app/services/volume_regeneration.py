"""Compare-and-replace volume plans without holding a transaction during LLM work."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.models.project import Project, Volume, Outline, Chapter, ChapterVersion
from app.services.budget_allocator import allocate_even
from app.services.outline_consistency_gate import validate_outline_consistency
from app.services.outline_generator import OutlineGenerator
from app.services.outline_order import volume_matches, stub_payload, prepare_author_volume, bind_chapter_content


class VolumeChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class VolumeSnapshot:
    project_id: UUID
    volume_id: UUID
    book_id: UUID
    volume_idx: int
    book_data: dict
    digest: str


def _values(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def _read(db, project_id, volume_id, *, lock=False):
    def query(model, *conditions):
        q = select(model).where(*conditions).execution_options(populate_existing=True)
        return q.with_for_update() if lock else q
    project_id, volume_id = UUID(str(project_id)), UUID(str(volume_id))
    project = (await db.scalars(query(Project, Project.id == project_id))).one_or_none()
    volume = (await db.scalars(query(Volume, Volume.id == volume_id, Volume.project_id == project_id))).one_or_none()
    if project is None or project.deleted_at is not None or volume is None:
        raise LookupError('作品或分卷不存在')
    book = (await db.scalars(query(Outline, Outline.project_id == project_id,
                                   Outline.level == 'book', Outline.is_confirmed == 1))).one_or_none()
    if book is None:
        raise ValueError('请先确认全书大纲')
    chapters = list((await db.scalars(query(Chapter, Chapter.volume_id == volume_id).order_by(Chapter.id))).all())
    outlines = list((await db.scalars(query(Outline, Outline.project_id == project_id,
                                           Outline.level == 'volume', Outline.parent_id == book.id).order_by(Outline.id))).all())
    outlines = [o for o in outlines if volume_matches(o.content_json, volume.id, volume.volume_idx)]
    versions = list((await db.scalars(query(ChapterVersion, ChapterVersion.chapter_id.in_([c.id for c in chapters])).order_by(ChapterVersion.id))).all())
    data = {'project': _values(project), 'volume': _values(volume), 'book': _values(book),
            'chapters': [_values(c) for c in chapters], 'versions': [_values(v) for v in versions], 'outlines': [_values(o) for o in outlines]}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    snapshot = VolumeSnapshot(project_id, volume_id, book.id, volume.volume_idx, deepcopy(book.content_json or {}), digest)
    return snapshot, volume, chapters, outlines


async def snapshot_volume(db, project_id, volume_id):
    snapshot, *_ = await _read(db, project_id, volume_id)
    return snapshot


def validate_replacement(parsed, snapshot):
    if not isinstance(parsed, dict) or parsed.get('_parse_error'):
        raise ValueError('分卷大纲格式错误，旧内容已保留')
    if type(parsed.get('volume_idx')) is not int or parsed['volume_idx'] != snapshot.volume_idx:
        raise ValueError('生成的卷号与目标分卷不符，旧内容已保留')
    count = parsed.get('chapter_count')
    if type(count) is not int or count <= 0:
        raise ValueError('分卷缺少有效章节数，旧内容已保留')
    OutlineGenerator._validate_volume_batch(parsed.get('chapter_summaries'), 1, count)
    for key in ('title', 'core_conflict'):
        if not isinstance(parsed.get(key), str) or not parsed[key].strip():
            raise ValueError('分卷缺少有效标题或核心内容，旧内容已保留')
    validate_outline_consistency(parsed, level='volume')


async def replace_volume(db, snapshot, parsed):
    """Caller owns commit. Parent locks also prevent new FK children during compare/delete."""
    validate_replacement(parsed, snapshot)
    from app.services.chapter_order import lock_chapter_structure, reindex_chapter_structure
    await lock_chapter_structure(db, snapshot.project_id, writer_owned=True)
    current, volume, chapters, outlines = await _read(db, snapshot.project_id, snapshot.volume_id, lock=True)
    if current.digest != snapshot.digest:
        raise VolumeChanged('生成期间资料已变化，旧内容已保留，请检查后重试')
    for chapter in chapters:
        await db.delete(chapter)
    for outline in outlines:
        await db.delete(outline)
    await db.flush()
    content = {**deepcopy(parsed), 'volume_id': str(volume.id),
               'chapter_summaries': [stub_payload(item) for item in parsed['chapter_summaries']]}
    outline = Outline(project_id=snapshot.project_id, level='volume', parent_id=snapshot.book_id, content_json=content)
    db.add(outline)
    volume.title = parsed['title'].strip()
    volume.summary = parsed['core_conflict'].strip()
    targets = allocate_even(int(volume.target_word_count), parsed['chapter_count'])
    created = []
    for item, target in zip(content['chapter_summaries'], targets):
        chapter = Chapter(volume_id=volume.id, title=item['title'].strip(), chapter_idx=item['chapter_idx'],
                          outline_json=deepcopy(item), target_word_count=target, global_idx=0)
        created.append(chapter)
        db.add(chapter)
    await db.flush()
    outline.content_json = prepare_author_volume({}, content, volume, created)
    for chapter in created:
        chapter.outline_json = bind_chapter_content(chapter.outline_json, chapter)
    await db.flush()
    await reindex_chapter_structure(db, snapshot.project_id)
    return {'chapters_created': len(targets), 'volume_target': volume.target_word_count, 'chapter_word_counts': targets}
