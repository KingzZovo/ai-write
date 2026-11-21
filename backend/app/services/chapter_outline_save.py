"""Snapshot and atomically save streamed chapter plans without replacing prose."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID

from sqlalchemy import select, tuple_

from app.models.project import Chapter, Outline, Project, Volume
from app.services.chapter_outline_expander import _validate_and_normalize, _validate_expansion_payload
from app.services.chapter_target_words import (
    resolve_chapter_target_word_count,
    resolve_chapter_words,
)
from app.services.summary_provenance import summary_is_current
from app.services.outline_authority import TEXT_REVISION, outline_for_generation
from app.services.outline_order import bind_chapter_content, chapter_stub


@dataclass(frozen=True)
class ChapterOutlineSnapshot:
    project_id: UUID
    parent_id: UUID
    chapter_idx: int
    chapter_id: UUID
    book_data: dict
    volume_data: dict
    previous_summary: str
    target_words: int
    digest: str
    chapter_plan: dict


def _values(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns} if row else None


async def _read(db, project_id, parent_id, chapter_idx, *, lock=False):
    project_id, parent_id = UUID(str(project_id)), UUID(str(parent_id))
    if type(chapter_idx) is not int or chapter_idx < 1:
        raise ValueError('请选择有效章号')

    def query(model, *conditions):
        stmt = select(model).where(*conditions).execution_options(populate_existing=True)
        return stmt.with_for_update() if lock else stmt

    project = await db.scalar(query(Project, Project.id == project_id))
    if project is None or project.deleted_at is not None:
        raise ValueError('作品不存在')
    parent = await db.scalar(query(Outline, Outline.id == parent_id,
                                  Outline.project_id == project_id, Outline.level == 'volume',
                                  Outline.is_confirmed == 1))
    if parent is None:
        raise ValueError('请先确认本作品的分卷大纲')
    book_query = query(Outline, Outline.project_id == project_id, Outline.level == 'book', Outline.is_confirmed == 1)
    if parent.parent_id:
        book_query = book_query.where(Outline.id == parent.parent_id)
    else:
        book_query = book_query.order_by(Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc()).limit(1)
    book = await db.scalar(book_query)
    if book is None:
        raise ValueError('分卷缺少本作品已确认的全书大纲')
    content = parent.content_json or {}
    volume_query = query(Volume, Volume.project_id == project_id)
    if content.get('volume_id') is not None:
        volume_query = volume_query.where(Volume.id == UUID(str(content['volume_id'])))
    elif type(content.get('volume_idx')) is int:
        volume_query = volume_query.where(Volume.volume_idx == content['volume_idx'])
    else:
        raise ValueError('分卷大纲缺少有效分卷标识')
    volumes = (await db.scalars(volume_query)).all()
    if len(volumes) != 1:
        raise ValueError('目标分卷不存在或卷号重复，请先整理')
    volume = volumes[0]
    chapters = (await db.scalars(query(Chapter, Chapter.volume_id == volume.id,
                                      Chapter.chapter_idx == chapter_idx))).all()
    if len(chapters) != 1:
        raise ValueError('目标章节不存在或章号重复，请先整理')
    chapter = chapters[0]
    previous_query = query(Chapter).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == project_id,
        tuple_(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        < (volume.volume_idx, volume.id, chapter.chapter_idx, chapter.id),
    ).order_by(Volume.volume_idx.desc(), Volume.id.desc(), Chapter.chapter_idx.desc(), Chapter.id.desc()).limit(1)
    prev = await db.scalar(previous_query)
    outlines = (await db.scalars(query(Outline, Outline.project_id == project_id,
                                      Outline.level == 'chapter').order_by(Outline.id))).all()
    def matches(outline):
        data = outline.content_json or {}
        if 'chapter_id' in data:
            return str(data['chapter_id']) == str(chapter.id)
        return outline.parent_id == parent.id and str(data.get('chapter_idx')) == str(chapter_idx)
    outlines = [outline for outline in outlines if matches(outline)]
    data = {'project': _values(project), 'book': _values(book), 'parent': _values(parent),
            'volume': _values(volume), 'chapter': _values(chapter), 'previous': _values(prev),
            'outlines': [_values(outline) for outline in outlines]}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    target = resolve_chapter_target_word_count(
        chapter.target_word_count, resolve_chapter_words(settings)
    )
    previous_summary = prev.summary.strip() if prev and prev.status == 'completed' and summary_is_current(prev) else ''
    plan = chapter.outline_json if isinstance(chapter.outline_json, dict) and chapter.outline_json else (
        chapter_stub(content, chapter) if not content.get(TEXT_REVISION) else {})
    plan = outline_for_generation(bind_chapter_content(plan, chapter))
    snapshot = ChapterOutlineSnapshot(project.id, parent.id, chapter_idx, chapter.id,
        deepcopy(book.content_json or {}), {**deepcopy(content), 'volume_id': str(volume.id), 'volume_idx': volume.volume_idx},
        previous_summary, target, digest, plan)
    return snapshot, chapter, outlines


async def snapshot_chapter_outline(db, project_id, parent_id, chapter_idx):
    snapshot, *_ = await _read(db, project_id, parent_id, chapter_idx)
    return snapshot


async def save_chapter_outline(db, snapshot, raw_text):
    """Caller owns the transaction; no partial plan or best-effort chapter update."""
    raw = raw_text.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = '\n'.join(raw.splitlines()[1:-1])
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError('章节细纲必须是完整JSON对象')
    _validate_expansion_payload(parsed)
    if 'chapter_idx' in parsed and (type(parsed['chapter_idx']) is not int or parsed['chapter_idx'] != snapshot.chapter_idx):
        raise ValueError('生成章号与目标章节不符')
    current, chapter, outlines = await _read(db, snapshot.project_id, snapshot.parent_id, snapshot.chapter_idx, lock=True)
    if current.chapter_id != snapshot.chapter_id or current.digest != snapshot.digest:
        raise ValueError('生成期间书纲、分卷或章节已修改，原稿已保留，请刷新检查')
    content = _validate_and_normalize(parsed, chapter, chapter.outline_json or {}, target_word_count=snapshot.target_words)
    title = parsed.get('title')
    written_title = None
    if isinstance(title, str) and 2 <= len(title.strip()) <= 30 and not re.fullmatch(r'第\d+章|\d+', title.strip()):
        if title.strip() != chapter.title:
            chapter.title = title.strip()
            written_title = chapter.title
    content.update(chapter_idx=chapter.chapter_idx, chapter_id=str(chapter.id),
                   volume_id=str(chapter.volume_id), title=chapter.title, raw_text=raw_text)
    content = bind_chapter_content(content, chapter)
    chapter.outline_json = content
    outline = Outline(project_id=snapshot.project_id, parent_id=snapshot.parent_id, level='chapter',
                      version=max((o.version or 1 for o in outlines), default=0) + 1, content_json=content)
    db.add(outline)
    await db.flush()
    return outline, written_title
