"""Durable payload delivery for settings; no model calls in replay."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.entity_extraction import OutlineSettingsExtraction as Receipt
from app.models.project import Character
from app.services.outline_settings_source import SettingsSource, SettingsSourceError, require_current_settings_source


def now():
    return datetime.now(timezone.utc)


async def find_receipt(db, source):
    return await db.scalar(select(Receipt).where(Receipt.outline_id == source.outline_id,
        Receipt.project_id == source.project_id, Receipt.source_hash == source.digest)
        .execution_options(populate_existing=True))


async def save_receipt(db, source, payload):
    await require_current_settings_source(db, source)
    receipt = await find_receipt(db, source)
    if receipt is None:
        receipt = Receipt(project_id=source.project_id, outline_id=source.outline_id,
            source_hash=source.digest, payload_json=deepcopy(payload), status='pending', attempts=0,
            next_attempt_at=now())
        db.add(receipt)
        await db.flush()
    await db.commit()  # Must be durable before the first graph write.
    return receipt


async def apply_payload(db, source, payload, *, driver=None, materialize=None):
    from app.services.outline_settings_graph import write_settings_graph
    if driver is None:
        from app.db.neo4j import init_neo4j
        from app.db import neo4j
        await init_neo4j()
        driver = neo4j._driver
    if driver is None:
        raise RuntimeError('graph_unavailable')
    if materialize is None:
        from app.tasks.entity_tasks import _materialize_entities_to_postgres
        materialize = _materialize_entities_to_postgres
    rows = await db.execute(select(Character).where(Character.project_id == source.project_id))
    existing = {character.name: character for character in rows.scalars().all()}
    await write_settings_graph(driver, source, payload, existing)
    return await materialize(project_id=str(source.project_id), chapter_idx=0,
        caller='outline_settings.delivery', strict=True, graph_driver=driver, preserve_existing_profiles=True)


async def deliver_receipt(db, receipt, *, apply=apply_payload, due_only=False):
    if receipt.status == 'completed':
        return receipt
    source = SettingsSource(receipt.project_id, receipt.outline_id, '', receipt.source_hash)
    try:
        source = await require_current_settings_source(db, source)
    except SettingsSourceError:
        # No graph side effects for an obsolete payload.
        receipt = await db.scalar(select(Receipt).where(Receipt.id == receipt.id)
            .with_for_update().execution_options(populate_existing=True))
        if receipt is None or receipt.status == 'completed':
            return receipt
        receipt.status = 'stale'
        receipt.last_error = 'source_changed'
        await db.commit()
        return receipt
    # Project/source locks serialize deliveries and official source edits. Re-read
    # after waiting; another process may already have completed this receipt.
    receipt = await db.scalar(select(Receipt).where(Receipt.id == receipt.id)
        .with_for_update().execution_options(populate_existing=True))
    if receipt is None or receipt.status == 'completed':
        return receipt
    # A second periodic worker may have selected this row before the first
    # worker failed and rescheduled it. Respect the updated deadline under lock.
    if due_only and (receipt.status not in ('pending', 'failed') or receipt.next_attempt_at > now()):
        return receipt
    receipt.attempts += 1
    try:
        counts = await apply(db, source, receipt.payload_json)
    except Exception:
        receipt.status = 'failed'
        receipt.last_error = 'delivery_failed'
        receipt.next_attempt_at = now() + timedelta(seconds=min(3600, 30 * 2 ** min(receipt.attempts, 7)))
    else:
        receipt.status = 'completed'
        receipt.counts_json = counts
        receipt.last_error = None
    await db.commit()
    return receipt


async def replay_settings(sessions, *, limit=20, apply=apply_payload):
    async with sessions() as db:
        ids = (await db.scalars(select(Receipt.id).where(Receipt.status.in_(['pending', 'failed']),
            Receipt.next_attempt_at <= now()).order_by(Receipt.next_attempt_at, Receipt.id).limit(limit))).all()
    outcomes = []
    for receipt_id in ids:
        try:
            async with asyncio.timeout(120):
                async with sessions() as db:
                    receipt = await db.get(Receipt, receipt_id)
                    if receipt:
                        result = await deliver_receipt(db, receipt, apply=apply, due_only=True)
                        outcomes.append(result.status if result else 'missing')
        except Exception:
            # The durable row remains pending/failed if a connection/commit dies.
            outcomes.append('retry_later')
    return {'examined': len(ids), 'completed': outcomes.count('completed'), 'outcomes': outcomes}
