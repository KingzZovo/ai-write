"""Durable extraction/replay with a per-project connection lease and graph fencing."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from uuid import UUID
from sqlalchemy import select, update, text, case
from sqlalchemy.dialects.postgresql import insert
from app.models.entity_extraction import ChapterEntityExtraction as Record, entity_projection_tokens
from app.services.entity_sources import resolve_entity_source
from app.services.chapter_entity_extraction import extract_entity_changes, validate_entity_changes
from app.services.entity_graph_projection import project_entity_payload, ENTITY_PROJECTION_VERSION, ProjectionSuperseded
from app.services.relationship_scope_delivery import (
    COMPLETE, SCOPE_PENDING, EntitySourceChanged, InvalidRetainedPayload,
    checkpoint_extraction, complete_pending_scope, extraction_payload_hash, validate_retained_payload,
)
from app.services.production.locking import WorkerLease


class EntityProjectionBusy(RuntimeError):
    pass


@asynccontextmanager
async def entity_project_lock(engine, project_id):
    key = int.from_bytes(hashlib.blake2b(b'aiwrite:entity:' + UUID(str(project_id)).bytes, digest_size=8).digest(), 'big', signed=True)
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}))
        await conn.commit()
        try:
            yield WorkerLease(acquired, conn)
        finally:
            if acquired:
                await conn.invalidate()


def now():
    return datetime.now(timezone.utc)


async def ensure_record(sessions, source):
    async with sessions() as db:
        await db.execute(insert(Record).values(
            project_id=source.project_id, chapter_id=source.chapter_id,
            source_hash=source.source_hash, source_json=source.receipt, status='pending',
        ).on_conflict_do_nothing(constraint='uq_chapter_entity_source'))
        record_id = await db.scalar(select(Record.id).where(
            Record.chapter_id == source.chapter_id, Record.source_hash == source.source_hash))
        await db.commit()
        return record_id


async def run_entity_projection(*, source, sessions, engine, driver, caller,
                                materialize, memory_cards, delivery_id=None):
    record_id = await ensure_record(sessions, source)
    async with entity_project_lock(engine, source.project_id) as lease:
        if not lease:
            raise EntityProjectionBusy('another entity worker owns this project')
        async with sessions() as db:
            current = await resolve_entity_source(db, source.project_id, 0, source.chapter_id)
            if current != source:
                await db.execute(update(Record).where(Record.id == record_id).values(status='obsolete', updated_at=now()))
                await db.commit()
                return {'status': 'skipped', 'reason': 'stale_source'}
            row = await db.get(Record, record_id)
            if (caller == 'entity_projection_replay'
                    and (row.status == 'scope_failed' or row.status == 'failed' and row.payload_json is None)
                    and (not delivery_id or row.delivery_id != delivery_id)):
                # Already queued periodic deliveries must not start a fresh
                # paid retry budget after another task's model failure. The
                # original Celery task may still use its own finite retries.
                return {'status': 'skipped', 'reason': 'model_retry_requires_original_task'}
            token = await db.scalar(entity_projection_tokens.next_value())
            payload, payload_hash, previous_status = row.payload_json, row.payload_hash, row.status
            payload_stage, scope = row.payload_stage, row.scope_json
            previous_version = row.projection_version
            row.projection_token = token
            row.delivery_id = delivery_id
            row.status = 'running'
            row.last_error = None
            row.updated_at = now()
            await db.commit()

        async def check_current():
            await checkpoint_extraction(source=source, sessions=sessions, lease=lease, record_id=record_id, token=token)

        async def save(data, stage, plan):
            await checkpoint_extraction(source=source, sessions=sessions, lease=lease, record_id=record_id, token=token,
                values=dict(payload_json=data, payload_stage=stage, scope_json=plan,
                    payload_hash=extraction_payload_hash(source.source_hash, data, stage, plan),
                    status='ready' if stage == COMPLETE else 'running'))

        try:
            await lease.check()
            if previous_status in ('completed', 'ready') and payload_stage != COMPLETE:
                raise InvalidRetainedPayload('partial_extraction_marked_complete')
            retained = validate_retained_payload(source, payload, payload_hash, payload_stage, scope,
                                                require_payload=previous_status in ('completed', 'ready'))
            if not retained:
                await check_current()
                async with sessions() as db:
                    payload = await extract_entity_changes(project_id=source.project_id,
                        chapter_text=source.content, db=db, defer_relationship_scope=True)
                validate_entity_changes(payload, source.content, defer_relationship_scope=True)
                payload_stage, scope = SCOPE_PENDING, None
                await save(payload, payload_stage, scope)
            if payload_stage == SCOPE_PENDING:
                payload = await complete_pending_scope(source=source, payload=payload, scope=scope,
                    sessions=sessions, driver=driver, save=save, check_current=check_current)
            payload_hash = extraction_payload_hash(source.source_hash, payload)
            validate_entity_changes(payload, source.content)
            await check_current()
            # No model IO under row locks. Author edits wait only for projection.
            async with sessions() as db:
                current = await resolve_entity_source(db, source.project_id, 0, source.chapter_id, lock=True)
                if current != source:
                    await db.execute(update(Record).where(Record.id == record_id, Record.projection_token == token)
                                     .values(status='obsolete', updated_at=now()))
                    await db.commit()
                    return {'status': 'skipped', 'reason': 'stale_source'}
                await lease.check()
                written = await project_entity_payload(driver, source, payload, payload_hash, token)
                await lease.check()
                materialized = previous_status != 'completed' or previous_version < ENTITY_PROJECTION_VERSION or written
                if materialized:
                    await materialize(project_id=source.project_id, chapter_idx=source.global_idx, caller=caller, strict=True, graph_driver=driver)
                    await memory_cards(project_id=source.project_id, global_idx=source.global_idx, chapter_text=source.content,
                                       source_chapter_id=source.chapter_id, source_hash=source.source_hash)
                await lease.check()
                result = await db.execute(update(Record).where(Record.id == record_id, Record.projection_token == token)
                    .values(status='completed', projection_version=ENTITY_PROJECTION_VERSION, updated_at=now()))
                if result.rowcount != 1:
                    raise ProjectionSuperseded('entity completion token no longer owns this extraction')
                await db.commit()
            return {'status': 'ok' if materialized else 'skipped',
                    'reason': 'projected' if written else ('replayed' if materialized else 'already_completed'),
                    'global_idx': source.global_idx, 'source_hash': source.source_hash,
                    'projection_token': token}
        except EntitySourceChanged:
            return {'status': 'skipped', 'reason': 'stale_source'}
        except Exception as exc:
            async with sessions() as db:
                values = dict(status=case((Record.payload_stage == SCOPE_PENDING, 'scope_failed'), else_='failed'),
                              last_error=type(exc).__name__, updated_at=now())
                if isinstance(exc, InvalidRetainedPayload):
                    # Retain the bad material for diagnosis, but do not requeue
                    # it or silently replace paid cached results with new ones.
                    values.update(status='failed', payload_hash=None)
                await db.execute(update(Record).where(Record.id == record_id, Record.projection_token == token)
                    .values(**values))
                await db.commit()
            raise


async def replay_pending_extractions(sessions, *, limit=50):
    """Recover queued/crashed work and retained payloads without model-failure loops.

    Scope failures stay outside this periodic loop; Celery retries are finite.
    Crashed workers may resume partial batches. Complete graph/read-model
    payloads can replay without model IO.
    """
    from datetime import timedelta
    from sqlalchemy import and_, or_
    from app.services.entity_dispatch import dispatch_entity_extraction
    cutoff, crashed = now() - timedelta(seconds=60), now() - timedelta(minutes=10)
    async with sessions() as db:
        rows = (await db.execute(select(Record.id, Record.project_id, Record.chapter_id, Record.source_json, Record.source_hash,
            Record.status, Record.projection_version, Record.payload_json, Record.payload_hash,
            Record.payload_stage, Record.scope_json, Record.projection_token, Record.updated_at)
            .where(Record.updated_at < cutoff, or_(
                Record.status == 'pending',
                and_(Record.status == 'completed', Record.projection_version < ENTITY_PROJECTION_VERSION, Record.payload_hash.is_not(None)),
                and_(Record.status.in_(['ready', 'failed']), Record.payload_stage == COMPLETE, Record.payload_hash.is_not(None)),
                and_(Record.status == 'running', Record.updated_at < crashed),
            )).order_by(Record.updated_at, Record.id).limit(min(max(limit, 1), 50)))).all()
    queued = 0
    for row in rows:
        async with sessions() as db:
            current = await resolve_entity_source(db, row.project_id, 0, row.chapter_id)
        if current is None or current.source_hash != row.source_hash:
            # Retire the old work item; persist its replacement before sending,
            # so a broker failure cannot lose the current source's pending work.
            if current is not None:
                await ensure_record(sessions, current)
            async with sessions() as db:
                await db.execute(update(Record).where(Record.id == row.id, Record.projection_token == row.projection_token,
                    Record.status == row.status, Record.updated_at == row.updated_at)
                                 .values(status='obsolete', updated_at=now()))
                await db.commit()
        if current is not None and current.source_hash == row.source_hash:
            # Both partial resumes and format upgrades need their own receipt.
            # Corruption never turns replay into unbounded paid extraction.
            try:
                if row.status in ('completed', 'ready') and row.payload_stage != COMPLETE:
                    raise InvalidRetainedPayload('partial_extraction_marked_complete')
                validate_retained_payload(current, row.payload_json, row.payload_hash, row.payload_stage, row.scope_json,
                                          require_payload=row.status in ('completed', 'ready'))
            except (ValueError, TypeError):
                async with sessions() as db:
                    await db.execute(update(Record).where(Record.id == row.id, Record.status == row.status,
                        Record.projection_token == row.projection_token, Record.updated_at == row.updated_at,
                        Record.payload_hash == row.payload_hash)
                        .values(status='failed', payload_hash=None,
                                last_error='InvalidRetainedPayload', updated_at=now()))
                    await db.commit()
                continue
        if current is not None:
            queued += bool(dispatch_entity_extraction(project_id=current.project_id, chapter_id=current.chapter_id,
                chapter_idx=current.receipt['chapter_idx'], caller='entity_projection_replay'))
    return {'examined': len(rows), 'queued': queued}
