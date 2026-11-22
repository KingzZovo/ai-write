"""Current saved-source cognition reads shared by the author UI and context."""
import logging
from uuid import UUID

from sqlalchemy import select, func

from app.models.cognition import ChapterCognition as Record, CognitionRevision as Revision
from app.models.project import Chapter, Volume, Project
from app.services.character_cognition import _validate_change_shape
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, resolve_entity_sources

logger = logging.getLogger(__name__)


class CognitionNotFound(Exception):
    pass


async def require_project(db, project_id, *, lock=False):
    stmt = select(Project.id).where(Project.id == UUID(str(project_id)), Project.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update(read=True, key_share=True)
    if (await db.execute(stmt.execution_options(autoflush=False))).scalar_one_or_none() is None:
        raise CognitionNotFound('作品不存在')


async def resolve_cognition_source(db, project_id, chapter_id):
    """Lock parent before children/FK writes, matching structure/deletion order."""
    from app.services.entity_sources import resolve_entity_source
    try:
        await require_project(db, project_id, lock=True)
    except CognitionNotFound:
        return None
    return await resolve_entity_source(db, project_id, 0, chapter_id, lock=True)


def validate_completed_receipt(record, source):
    """Validate the saved delta's shape and receipt; not an evidence proof."""
    if record is None or record.status != 'completed':
        # 兜底文案("人物认知材料暂不可用")对应多个错误码,且这一条是长篇最容易
        # 反复撞的墙 —— 不点名是哪一章、哪条记录、什么状态,排查只能靠猜。
        logger.warning(
            "character_cognition_not_ready chapter=%s record=%s status=%s source_hash=%s",
            getattr(source, 'chapter_id', None),
            'missing' if record is None else 'present',
            getattr(record, 'status', None),
            str(getattr(source, 'source_hash', '') or '')[:12],
        )
        raise RequiredContextUnavailable('character_cognition_not_ready')
    try:
        if (record.source_hash != source.source_hash or record.source_json != source.receipt
                or record.payload_hash != digest([source.source_hash, record.changes_json])):
            raise ValueError('invalid_cognition_receipt')
        _validate_change_shape(record.changes_json)
    except (ValueError, TypeError) as exc:
        raise RequiredContextUnavailable('character_cognition_payload_invalid') from exc


def validate_completed(record, source):
    """Validate the entire saved delta, including a legitimate empty result."""
    from app.services.chapter_entity_extraction import evidence_locatable
    validate_completed_receipt(record, source)
    if any(not evidence_locatable(item['evidence'], source.content)
           for item in record.changes_json):
        raise RequiredContextUnavailable('character_cognition_evidence_unavailable')


def revision_payload(revision):
    """Canonical immutable decision, excluding the database-assigned timestamp."""
    return dict(version=1, id=str(revision.id), project_id=str(revision.project_id),
        chapter_id=str(revision.chapter_id), record_id=str(revision.record_id),
        previous_id=str(revision.previous_id) if revision.previous_id else None,
        source_hash=revision.source_hash, source_json=revision.source_json,
        base_version=revision.base_version, mode=revision.mode, changes=revision.changes_json,
        model_snapshot=revision.model_snapshot_json, actor=revision.actor)


def validate_revision(revision):
    if revision is None:
        raise RequiredContextUnavailable('character_cognition_author_decision_invalid')
    try:
        if revision.payload_hash != digest(revision_payload(revision)):
            raise ValueError('invalid_decision_receipt')
        if revision.mode == 'author':
            _validate_change_shape(revision.changes_json)
        elif revision.mode != 'auto' or revision.changes_json is not None:
            raise ValueError('invalid_decision_mode')
    except (ValueError, TypeError, AttributeError) as exc:
        raise RequiredContextUnavailable('character_cognition_author_decision_invalid') from exc


def validate_decision(record, source, revision=None):
    """Validate ownership independently of whether automatic work is ready."""
    mode = record.source_mode
    if (mode not in {'auto', 'author'} or (mode == 'author' and record.author_revision_id is None)
            or record.source_hash != source.source_hash or record.source_json != source.receipt):
        raise RequiredContextUnavailable('character_cognition_author_decision_invalid')
    if record.author_revision_id is not None:
        validate_revision(revision)
        if (revision.id != record.author_revision_id or revision.record_id != record.id
                or revision.project_id != record.project_id or revision.chapter_id != record.chapter_id
                or revision.mode != mode or revision.source_hash != source.source_hash
                or revision.source_json != source.receipt):
            raise RequiredContextUnavailable('character_cognition_author_decision_invalid')


def _degraded_continuation_enabled() -> bool:
    """上一章认知未就绪时,是否允许继续往下写。

    默认关闭 = 维持现状(未就绪阻断)。打开后只影响"提取本身没成功"这一类软失败,
    不会放过凭据不符/决定被改这类真实损坏(见下面 status 必须是未完成才降级)。
    """
    try:
        from app.config import settings

        return bool(getattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", False))
    except Exception:  # noqa: BLE001
        return False


def _degrade(chapter_id, status):
    logger.warning("cognition_degraded_continuation chapter=%s status=%s;"
                   "该章按「无认知变化」计入,建议事后补跑提取", chapter_id, status)
    return []


def effective_change_payload(record, source, revision=None):
    """Select a receipt-checked payload; full-body evidence is checked separately."""
    if record is None:
        if _degraded_continuation_enabled():
            return _degrade(getattr(source, "chapter_id", None), "missing")
        raise RequiredContextUnavailable('character_cognition_not_ready')
    validate_decision(record, source, revision)
    if record.source_mode == 'author':
        return revision.changes_json
    try:
        validate_completed_receipt(record, source)
    except RequiredContextUnavailable:
        if (_degraded_continuation_enabled()
                and getattr(record, "status", None) != "completed"):
            return _degrade(getattr(source, "chapter_id", None),
                            getattr(record, "status", "unknown"))
        raise
    return record.changes_json


def effective_changes(record, source, revision=None):
    """An invalid author decision cannot silently fall back to model output."""
    from app.services.chapter_entity_extraction import evidence_locatable
    changes = effective_change_payload(record, source, revision)
    if any(not evidence_locatable(item['evidence'], source.content) for item in changes):
        raise RequiredContextUnavailable('character_cognition_evidence_unavailable')
    return changes


async def load_revisions(db, project_id, records):
    ids = [row.author_revision_id for row in records.values() if row.author_revision_id is not None]
    if not ids:
        return {}
    rows = (await db.execute(select(Revision).where(Revision.project_id == UUID(str(project_id)),
        Revision.id.in_(ids)).execution_options(autoflush=False, populate_existing=True))).scalars().all()
    return {row.id: row for row in rows}


def record_state(record, source, revision=None):
    if source is None:
        return 'unavailable'
    if record is None:
        return 'missing'
    try:
        validate_decision(record, source, revision)
        if record.source_mode == 'author' or record.status == 'completed':
            effective_changes(record, source, revision)
            return 'completed'
    except RequiredContextUnavailable:
        return 'invalid'
    return record.status if record.status in {'pending', 'queued', 'running', 'failed', 'obsolete', 'completed'} else 'invalid'


def record_version(record, source):
    return digest(dict(source_hash=source.source_hash, record_id=str(record.id) if record else None,
        cycle_id=str(record.cycle_id) if record else None, status=record.status if record else None,
        attempts=record.attempt_count if record else None, payload_hash=record.payload_hash if record else None,
        source_mode=record.source_mode if record else 'auto',
        author_revision_id=str(record.author_revision_id) if record and record.author_revision_id else None,
        updated_at=record.updated_at.isoformat() if record and record.updated_at else None))


async def load_source_records(db, project_id, sources, *, lock=False):
    """Sources are a bounded batch; do not load every old version per chapter."""
    if not sources:
        return {}
    stmt = select(Record).where(Record.project_id == UUID(str(project_id)),
        Record.chapter_id.in_([UUID(cid) for cid in sources]),
        Record.source_hash.in_([source.source_hash for source in sources.values()])).order_by(Record.chapter_id)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt.execution_options(autoflush=False, populate_existing=True))).scalars().all()
    return {str(row.chapter_id): row for row in rows
            if row.source_hash == sources[str(row.chapter_id)].source_hash}


def _chapter_item(chapter, source, record, revision=None, *, include_changes=False, include_source=False):
    state = record_state(record, source, revision)
    mode = record.source_mode if record else 'auto'
    changes = effective_changes(record, source, revision) if state == 'completed' else None
    can_retry = mode == 'auto' and state in {'missing', 'failed', 'obsolete', 'invalid'}
    if can_retry and record is not None and record.author_revision_id is not None:
        try:
            validate_decision(record, source, revision)
        except RequiredContextUnavailable:
            can_retry = False
    messages = {
        'unavailable': '需有已完成的保存稿和有效章序，才能提取认知。',
        'missing': '当前保存稿尚未提取，可单章提取或补提取历史章节。',
        'failed': '本轮提取未完成。可重试当前保存稿。',
        'obsolete': '当前保存稿的旧任务已失效，可重新安排提取。',
        'invalid': '已有结果的完整性或原文依据未通过核对，可重新提取。',
        'completed': '原句可定位；仍需结合上下文核对人物是否确实获知。',
    }
    if mode == 'author':
        messages['completed'] = '使用作者校订；自动提取不会覆盖这份保存稿的认知。'
        messages['invalid'] = '作者校订的来源或完整性未通过核对，请重新校订或明确恢复自动提取。'
    elif state == 'invalid' and not can_retry:
        messages['invalid'] = '作者决定记录无法核对，请重新校订或明确恢复自动提取。'
    result = dict(chapter_id=str(chapter.id), chapter_title=source.receipt['title'] if source else chapter.title,
        global_idx=source.global_idx if source else chapter.global_idx,
        chapter_status=source.receipt['status'] if source else chapter.status,
        status=state, message=messages.get(state, '后台正在处理当前保存稿。'),
        record_id=str(record.id) if record else None, cycle_id=str(record.cycle_id) if record else None,
        source_hash=source.source_hash if source else None,
        version=record_version(record, source) if source else None,
        attempts=record.attempt_count if record else 0,
        source_mode=mode, revision_id=str(record.author_revision_id) if record and record.author_revision_id else None,
        changes_count=len(changes) if changes is not None else None, can_edit=source is not None,
        can_retry=can_retry)
    if include_changes:
        result['changes'] = changes
        result['model_status'] = record.status if record else 'missing'
        try:
            validate_completed(record, source)
            result['model_changes'] = record.changes_json
        except RequiredContextUnavailable:
            result['model_changes'] = None
        result['source_content'] = source.content if source and include_source else None
    return result


async def list_chapter_cognition(db, project_id, *, limit=20, offset=0):
    await require_project(db, project_id)
    where = Volume.project_id == UUID(str(project_id))
    rows = (await db.execute(select(Chapter.id, Chapter.title, Chapter.global_idx, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id).where(where)
        .order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
        .offset(offset).limit(limit + 1).execution_options(autoflush=False))).all()
    total = (await db.execute(select(func.count(Chapter.id)).join(Volume, Chapter.volume_id == Volume.id)
        .where(where).execution_options(autoflush=False))).scalar_one()
    visible = rows[:limit]
    sources = await resolve_entity_sources(db, project_id, [row.id for row in visible])
    records = await load_source_records(db, project_id, sources)
    revisions = await load_revisions(db, project_id, records)
    return dict(chapters=[_chapter_item(row, sources.get(str(row.id)), records.get(str(row.id)),
                            revisions.get(records[str(row.id)].author_revision_id) if str(row.id) in records else None)
                         for row in visible], total=total, has_more=len(rows) > limit)


async def read_chapter_cognition(db, project_id, chapter_id, *, include_source=False):
    await require_project(db, project_id)
    row = (await db.execute(select(Chapter.id, Chapter.title, Chapter.global_idx, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == UUID(str(project_id)),
            Chapter.id == UUID(str(chapter_id))).execution_options(autoflush=False))).one_or_none()
    if row is None:
        raise CognitionNotFound('该作品中未找到章节')
    sources = await resolve_entity_sources(db, project_id, [chapter_id])
    records = await load_source_records(db, project_id, sources)
    revisions = await load_revisions(db, project_id, records)
    record = records.get(str(chapter_id))
    return _chapter_item(row, sources.get(str(chapter_id)), record,
        revisions.get(record.author_revision_id) if record else None, include_changes=True, include_source=include_source)


async def list_revisions(db, project_id, chapter_id, *, limit=10, offset=0):
    """Historical decisions stay readable after a chapter is removed."""
    await require_project(db, project_id)
    rows = (await db.execute(select(Revision).where(Revision.project_id == UUID(str(project_id)),
        Revision.chapter_id == UUID(str(chapter_id))).order_by(Revision.created_at.desc(), Revision.id.desc())
        .offset(offset).limit(limit + 1).execution_options(autoflush=False))).scalars().all()
    sources = await resolve_entity_sources(db, project_id, [chapter_id])
    records = await load_source_records(db, project_id, sources)
    source, record = sources.get(str(chapter_id)), records.get(str(chapter_id))
    items = []
    for row in rows[:limit]:
        try:
            validate_revision(row)
            valid = True
        except RequiredContextUnavailable:
            valid = False
        items.append(dict(revision_id=str(row.id), previous_id=str(row.previous_id) if row.previous_id else None,
            source_hash=row.source_hash, mode=row.mode, changes=row.changes_json if valid else None,
            model_snapshot=row.model_snapshot_json if valid else None, actor=row.actor, created_at=row.created_at,
            valid=valid, matches_current_source=bool(source and row.source_hash == source.source_hash
                and row.source_json == source.receipt), is_current=bool(record and row.id == record.author_revision_id)))
    return dict(revisions=items, has_more=len(rows) > limit)
