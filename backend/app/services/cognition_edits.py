"""Append-only author corrections and explicit restoration of automatic work."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import insert

from app.models.cognition import CognitionRevision as Revision
from app.services.character_cognition import validate_changes
from app.services.cognition_reader import load_source_records, record_version, revision_payload, validate_completed
from app.services.cognition_requests import reserve_request, acknowledge_request, CognitionConflict
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, resolve_entity_sources
from app.services.entity_projection import now


class CognitionEditInvalid(Exception):
    pass


async def save_cognition_decision(db, project_id, chapter_id, request_id, source_hash, expected_version,
                                  *, mode, changes=None, actor=None):
    """Commit belongs to caller. Fixed requests precede source/record locks."""
    project_id, chapter_id = UUID(str(project_id)), UUID(str(chapter_id))
    if mode not in {'author', 'auto'} or (mode == 'auto' and changes is not None):
        raise CognitionEditInvalid('校订方式无效')
    operation = 'edit' if mode == 'author' else 'restore_auto'
    payload = dict(operation=operation, chapter_id=str(chapter_id), source_hash=source_hash,
        version=expected_version, changes=changes, actor=actor)
    repeated = await reserve_request(db, project_id, request_id, payload)
    if repeated is not None:
        return repeated
    sources = await resolve_entity_sources(db, project_id, [chapter_id], lock=True)
    source = sources.get(str(chapter_id))
    if source is None or source.source_hash != source_hash:
        raise CognitionConflict('当前保存稿或章序已变化，请保留校订草稿，刷新后重新核对原句')
    records = await load_source_records(db, project_id, sources, lock=True)
    row = records.get(str(chapter_id))
    if record_version(row, source) != expected_version:
        raise CognitionConflict('认知记录已变化，请保留校订草稿，刷新后重新核对')
    if mode == 'author':
        try:
            validate_changes(changes, source.content)
        except (TypeError, ValueError) as exc:
            raise CognitionEditInvalid(str(exc)) from exc
    if row is None:
        from app.services.cognition_extraction import enqueue_cognition
        await enqueue_cognition(db, source)
        row = (await load_source_records(db, project_id, sources, lock=True))[str(chapter_id)]
    model_snapshot = dict(status=row.status, changes=deepcopy(row.changes_json), payload_hash=row.payload_hash,
        source_hash=row.source_hash, source_json=deepcopy(row.source_json), cycle_id=str(row.cycle_id),
        attempts=row.attempt_count)
    values = dict(id=uuid4(), project_id=project_id, chapter_id=chapter_id, record_id=row.id,
        previous_id=row.author_revision_id, source_hash=source.source_hash, source_json=source.receipt,
        base_version=expected_version, mode=mode, changes_json=deepcopy(changes),
        model_snapshot_json=model_snapshot, actor=actor)
    values['payload_hash'] = digest(revision_payload(SimpleNamespace(**values)))
    await db.execute(insert(Revision).values(**values).execution_options(autoflush=False))
    # Even the same text is a new explicit decision. Late model replies lose
    # both their cycle and token; the original model payload stays untouched.
    row.cycle_id = uuid4()
    row.attempt_token = None
    row.delivery_id = None
    row.source_mode = mode
    row.author_revision_id = values['id']
    if mode == 'author':
        if row.status in {'pending', 'queued', 'running'}:
            row.status = 'obsolete'
        outcome = 'saved'
    else:
        try:
            validate_completed(row, source)
        except RequiredContextUnavailable:
            row.status = 'pending'
            row.attempt_count = 0
            row.last_error = None
            outcome = 'scheduled'
        else:
            outcome = 'already_complete'
    row.source_json = source.receipt
    row.updated_at = now()
    return await acknowledge_request(db, request_id, dict(operation=operation, outcome=outcome,
        chapter_id=str(chapter_id), record_id=str(row.id), cycle_id=str(row.cycle_id),
        source_hash=source.source_hash, revision_id=str(values['id']), source_mode=mode))
