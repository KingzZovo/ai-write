"""Author-accepted cycles and immutable request receipts in one transaction."""
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.cognition import ChapterCognition as Record, CognitionRequest as Receipt
from app.services.cognition_reader import (require_project, record_state, record_version, load_source_records,
                                          load_revisions, validate_decision)
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, resolve_entity_sources
from app.services.entity_projection import now


class CognitionConflict(Exception):
    pass


async def reserve_request(db, project_id, request_id, payload):
    """Reserve before source locks; an uncommitted placeholder never escapes."""
    project_id, request_id = UUID(str(project_id)), UUID(str(request_id))
    await require_project(db, project_id, lock=True)
    signature = digest(dict(version=1, project_id=str(project_id), payload=payload))
    inserted = (await db.execute(insert(Receipt).values(id=request_id, project_id=project_id,
        request_hash=signature).on_conflict_do_nothing(index_elements=['id']).returning(Receipt.id)
        .execution_options(autoflush=False))).scalar_one_or_none()
    row = (await db.execute(select(Receipt).where(Receipt.id == request_id).with_for_update()
        .execution_options(autoflush=False, populate_existing=True))).scalar_one()
    if row.project_id != project_id or row.request_hash != signature:
        raise CognitionConflict('该请求编号已用于其他内容，请刷新后重新提交')
    if inserted is None and not isinstance(row.result_json, dict):
        raise CognitionConflict('原请求的受理记录不完整，请核对任务状态')
    return row.result_json if inserted is None else None


async def acknowledge_request(db, request_id, result):
    result = dict(result, request_id=str(request_id))
    await db.execute(update(Receipt).where(Receipt.id == UUID(str(request_id))).values(result_json=result)
        .execution_options(autoflush=False, synchronize_session=False))
    return result


async def retry_cognition(db, project_id, chapter_id, request_id, source_hash, expected_version):
    """Caller commits before acknowledging; the fixed request owns any reset."""
    project_id, chapter_id = UUID(str(project_id)), UUID(str(chapter_id))
    payload = dict(operation='retry', chapter_id=str(chapter_id), source_hash=source_hash, version=expected_version)
    repeated = await reserve_request(db, project_id, request_id, payload)
    if repeated is not None:
        return repeated
    sources = await resolve_entity_sources(db, project_id, [chapter_id], lock=True)
    source = sources.get(str(chapter_id))
    if source is None or source.source_hash != source_hash:
        raise CognitionConflict('当前保存稿或章序已变化，请刷新后再提取')
    records = await load_source_records(db, project_id, sources, lock=True)
    row = records.get(str(chapter_id))
    if record_version(row, source) != expected_version:
        raise CognitionConflict('提取状态已变化，请刷新核对后再重试')
    revisions = await load_revisions(db, project_id, records)
    revision = revisions.get(row.author_revision_id) if row else None
    if row is not None:
        if row.source_mode != 'auto':
            raise CognitionConflict('当前使用作者校订，请通过恢复自动提取明确撤回校订')
        if row.author_revision_id is not None:
            try:
                validate_decision(row, source, revision)
            except RequiredContextUnavailable:
                raise CognitionConflict('作者决定记录无法核对，请重新校订或明确恢复自动提取') from None
    state = record_state(row, source, revision)
    outcome = 'already_complete' if state == 'completed' else 'already_active'
    if row is None:
        from app.services.cognition_extraction import enqueue_cognition
        await enqueue_cognition(db, source)
        row = (await load_source_records(db, project_id, sources))[str(chapter_id)]
        outcome = 'scheduled'
    elif state in {'failed', 'obsolete', 'invalid'}:
        row.cycle_id = uuid4()
        row.status = 'pending'
        row.attempt_count = 0
        row.attempt_token = None
        row.delivery_id = None
        row.last_error = None
        row.changes_json = None
        row.payload_hash = None
        row.source_json = source.receipt
        row.updated_at = now()
        outcome = 'scheduled'
    return await acknowledge_request(db, request_id, dict(operation='retry', outcome=outcome,
        chapter_id=str(chapter_id), record_id=str(row.id), cycle_id=str(row.cycle_id), source_hash=source.source_hash))
