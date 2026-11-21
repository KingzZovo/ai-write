"""Bounded delivery of durable batches through the existing control queue."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import and_, or_, select

from app.models.generation_batch import GenerationBatch
from app.models.project import Project
from app.services.batch_execution import EXECUTION_VERSION, STALE_SECONDS

logger = logging.getLogger(__name__)
DISPATCH_SECONDS = 30


async def dispatch_batches(sessions, publish, *, limit=32, now=None):
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 100))
    eligible = or_(GenerationBatch.status == 'pending', and_(GenerationBatch.status == 'running',
        GenerationBatch.execution_version == EXECUTION_VERSION,
        GenerationBatch.worker_id.is_not(None),
        GenerationBatch.heartbeat_at <= now - timedelta(seconds=STALE_SECONDS)))
    due = or_(GenerationBatch.dispatched_at.is_(None),
              GenerationBatch.dispatched_at <= now - timedelta(seconds=DISPATCH_SECONDS))
    async with sessions() as db:
        candidates = list((await db.execute(select(GenerationBatch.id, GenerationBatch.project_id)
            .join(Project, Project.id == GenerationBatch.project_id)
            .where(eligible, due, Project.deleted_at.is_(None))
            .order_by(GenerationBatch.dispatched_at.asc().nullsfirst(), GenerationBatch.created_at, GenerationBatch.id)
            .limit(limit))).all())
    delivered = 0
    for candidate in candidates:
        try:
            async with sessions() as db:
                row = await db.scalar(select(GenerationBatch).where(GenerationBatch.id == candidate.id, eligible, due)
                    .with_for_update(skip_locked=True))
                if row is None:
                    continue
                row.dispatched_at = now
                await db.commit()
            # A crash or lost broker response retries this SAME request later.
            await publish(str(candidate.project_id), str(candidate.id))
            delivered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Batch delivery unconfirmed for %s; durable request retained', candidate.id)
    return delivered


async def publish_batch(project_id, request_id):
    from app.tasks.generation_tasks import run_generation_batch
    await asyncio.to_thread(run_generation_batch.apply_async, args=[project_id, request_id], retry=False)


async def dispatch_pending_batches():
    from app.db.session import async_session_factory
    return await dispatch_batches(async_session_factory, publish_batch)
