"""Bounded fingerprints of the author material an independent retry may replace.

This is target/source protection, not a frozen copy of the entire prompt,
knowledge graph or model routing. Every attempt still prepares fresh context.
"""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import and_, or_, select

from app.models.project import Chapter, Outline, Project, ReferenceBook, StyleProfile, Volume
from app.services.generation_submissions import chapter_submission_source
from app.services.outline_order import volume_condition


class TaskSourceUnavailable(ValueError):
    pass


def _json_default(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat(timespec='microseconds')
    raise TypeError('Unsupported generation source value')


def source_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False, default=_json_default).encode('utf-8')).hexdigest()


def row_digest(row):
    return None if row is None else source_digest({column.key: getattr(row, column.key) for column in row.__table__.columns})


async def _rows_digest(db, model, condition):
    """Read all matching rows in bounded pages; never limit a book's length."""
    checksum, count, last = hashlib.sha256(), 0, None
    while True:
        query = select(model).where(condition).order_by(model.id).limit(64)
        if last is not None: query = query.where(model.id > last)
        rows = list((await db.execute(query.execution_options(populate_existing=True, autoflush=False))).scalars())
        for row in rows:
            checksum.update(row_digest(row).encode('ascii'))
            count += 1
        if len(rows) < 64:
            return {'count': count, 'hash': checksum.hexdigest()}
        last = rows[-1].id


async def _optional_row(db, model, identity):
    if identity is None or identity == '':
        return None
    try:
        identity = UUID(str(identity))
    except ValueError:
        raise TaskSourceUnavailable('任务引用的来源编号无效。') from None
    row = await db.get(model, identity, populate_existing=True)
    return {'id': str(identity), 'hash': row_digest(row)}


async def capture_task_source(db, task, *, project=None, chapter=None):
    params = task.params_json if isinstance(task.params_json, dict) else {}
    project = project or await db.get(Project, task.project_id, populate_existing=True)
    if project is None or project.deleted_at is not None:
        raise TaskSourceUnavailable('任务的作品不存在或已删除。')
    source = {'project': row_digest(project)}
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    style_reference, structure = settings.get('style_reference'), settings.get('plot_structure')
    style_id = params.get('style_id') or settings.get('style_profile_id') or (
        style_reference.get('profile_id') if isinstance(style_reference, dict) else None)
    structure_id = params.get('structure_book_id') or (
        structure.get('structure_book_id') if isinstance(structure, dict) else None)
    source['style'] = await _optional_row(db, StyleProfile, style_id)
    source['structure'] = await _optional_row(db, ReferenceBook, structure_id)
    if task.task_type in {'chapter', 'outline_chapter'}:
        try:
            cid = UUID(str(params.get('chapter_id')))
        except ValueError:
            raise TaskSourceUnavailable('任务的章节编号无效。') from None
        chapter = chapter or await db.get(Chapter, cid, populate_existing=True)
        if chapter is None:
            raise TaskSourceUnavailable('任务的章节已删除。')
        volume = await db.get(Volume, chapter.volume_id, populate_existing=True)
        if chapter.id != cid or volume is None or volume.project_id != task.project_id:
            raise TaskSourceUnavailable('任务章节不属于原作品。')
        source['chapter'] = chapter_submission_source(chapter)
    elif task.task_type == 'outline_volume':
        from app.services.volume_outline_writes import require_volume_task_source
        volume = await require_volume_task_source(db, task.project_id, params)
    elif task.task_type in {'outline_book', 'outline_from_reference'}:
        volume = None
        source['volumes'] = await _rows_digest(db, Volume, Volume.project_id == task.project_id)
    else:
        raise TaskSourceUnavailable('任务类型无法核对原来源。')
    outline_scope = Outline.level == 'book'
    if volume is not None:
        source['volume'] = row_digest(volume)
        outline_scope = or_(outline_scope, and_(Outline.level == 'volume', volume_condition(volume.id, volume.volume_idx)))
    source['outlines'] = await _rows_digest(db, Outline, and_(Outline.project_id == task.project_id, outline_scope))
    if task.task_type == 'outline_from_reference':
        reference_id = params.get('reference_book_id')
        source['reference'] = await _optional_row(db, ReferenceBook, reference_id)
        if source['reference'] is not None and source['reference']['hash'] is not None:
            from app.services.outline_from_reference import _load_beat_sketch
            source['reference']['sketch_hash'] = source_digest(await _load_beat_sketch(str(reference_id), db))
    return {'version': 1, 'task_type': task.task_type, 'material_hash': source_digest(source)}


def valid_task_source(source, task_type):
    return (isinstance(source, dict) and set(source) == {'version', 'task_type', 'material_hash'}
        and type(source.get('version')) is int and source['version'] == 1 and source.get('task_type') == task_type
        and isinstance(source.get('material_hash'), str) and len(source['material_hash']) == 64
        and all(char in '0123456789abcdef' for char in source['material_hash']))
