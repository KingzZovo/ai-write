"""Durable relationship-scope checkpoints; model IO never holds author rows."""
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import update

from app.models.entity_extraction import ChapterEntityExtraction as Record
from app.services import prompt_cache
from app.services.chapter_entity_extraction import validate_entity_changes
from app.services.entity_context import verified_relationship_candidates
from app.services.entity_graph_projection import ProjectionSuperseded
from app.services.entity_sources import digest, resolve_entity_source
from app.services.relationship_scope import (
    TASK_TYPE, RelationshipScopeError, build_scope_plan, finish_scope_payload,
    review_scope_batch, validate_scope_plan,
)

COMPLETE = 'complete'
SCOPE_PENDING = 'scope_pending'


class EntitySourceChanged(RuntimeError):
    pass


class InvalidRetainedPayload(ValueError):
    pass


def extraction_payload_hash(source_hash, payload, stage=COMPLETE, scope=None):
    # Complete payloads retain the original graph/SQL receipt identity.
    if stage == COMPLETE:
        if scope is not None:
            raise InvalidRetainedPayload('complete_payload_has_pending_scope')
        return digest([source_hash, payload])
    if stage != SCOPE_PENDING:
        raise InvalidRetainedPayload('unknown_extraction_stage')
    return digest([source_hash, stage, payload, scope])


def validate_retained_payload(source, payload, payload_hash, stage, scope, *, require_payload=False):
    """A partial cache can neither masquerade as complete nor trigger re-extraction."""
    if payload is None:
        if require_payload or payload_hash is not None or stage != COMPLETE or scope is not None:
            raise InvalidRetainedPayload('incomplete_extraction_receipt')
        return False
    try:
        if payload_hash != extraction_payload_hash(source.source_hash, payload, stage, scope):
            raise InvalidRetainedPayload('invalid_extraction_payload_hash')
        validate_entity_changes(payload, source.content, defer_relationship_scope=stage == SCOPE_PENDING)
        if stage == SCOPE_PENDING and scope is not None:
            validate_scope_plan(payload, scope, source)
    except (ValueError, TypeError) as exc:
        raise InvalidRetainedPayload('invalid_retained_entity_payload') from exc
    return True


async def checkpoint_extraction(*, source, sessions, lease, record_id, token, values=None):
    """Recheck source and fencing token in a short transaction before accepting work."""
    await lease.check()
    async with sessions() as db:
        current = await resolve_entity_source(db, source.project_id, 0, source.chapter_id, lock=True)
        stale = current != source
        patch = {'status': 'obsolete'} if stale else deepcopy(values or {})
        patch['updated_at'] = datetime.now(timezone.utc)
        result = await db.execute(update(Record).where(
            Record.id == record_id, Record.projection_token == token).values(**patch))
        # A deleted chapter may already have cascaded its extraction receipt.
        # It is still a stale source; never rewrite another token's row.
        if result.rowcount != 1 and not stale:
            raise ProjectionSuperseded('entity checkpoint token no longer owns this extraction')
        await db.commit()
    if stale:
        raise EntitySourceChanged('chapter source changed during extraction')


async def complete_pending_scope(*, source, payload, scope, sessions, driver, save, check_current):
    """Freeze the full relevant history once, then resume only unfinished batches."""
    plan = deepcopy(scope)
    if plan is None:
        pairs = sorted({(item['char1'], item['char2']) for item in payload['changed_relationships']})
        candidates = []
        if pairs:
            async with sessions() as db:
                candidates = await verified_relationship_candidates(
                    db, driver, source.project_id, source.global_idx, pairs=pairs)
        system_chars = 0
        if candidates:
            async with sessions() as db:
                snapshot = await prompt_cache.get_snapshot(TASK_TYPE, db)
            if snapshot is None or snapshot.endpoint_id is None:
                raise RelationshipScopeError('relationship_scope_prompt_not_configured')
            system_chars = len(snapshot.system_prompt)
        plan = build_scope_plan(payload, candidates, source.content, system_chars=system_chars)
        validate_scope_plan(payload, plan, source)
        await save(payload, SCOPE_PENDING, plan)
    else:
        validate_scope_plan(payload, plan, source)
    for batch in plan['batches']:
        if batch['reviews'] is not None:
            continue
        await check_current()
        # Candidate/source reads and checkpoint transactions have ended before
        # the prompt runner borrows a separate session for route resolution.
        async with sessions() as db:
            reviews = await review_scope_batch(source, payload['changed_relationships'][batch['event_index']], batch, db)
        batch['reviews'] = reviews
        await save(payload, SCOPE_PENDING, plan)
    result = finish_scope_payload(payload, plan, source)
    await save(result, COMPLETE, None)
    return result
