"""Bounded redelivery of original independent task IDs, without reading prose."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import and_, or_, select, update

from app.models.generation_task import GenerationTask
from app.models.project import Project
from app.services.generation_task_execution import EXECUTION_VERSION
from app.services.generation_task_recovery import ACTIVE_TASK_STATUSES, STALE_SECONDS

logger = logging.getLogger(__name__)
DISPATCH_SECONDS = 30
TASK_TYPES = ('chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference')


async def dispatch_tasks(sessions, publish, *, limit=32, now=None):
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 100))
    eligible = or_(GenerationTask.status == 'pending', and_(
        GenerationTask.status.in_(ACTIVE_TASK_STATUSES), GenerationTask.execution_version == EXECUTION_VERSION,
        GenerationTask.execution_id.is_not(None),
        GenerationTask.heartbeat_at <= now - timedelta(seconds=STALE_SECONDS)))
    independent = and_(GenerationTask.task_type.in_(TASK_TYPES),
        GenerationTask.params_json['batch_request_id'].as_string().is_(None),
        GenerationTask.params_json['pipeline_id'].as_string().is_(None),
        GenerationTask.params_json['cancelled_at'].as_string().is_(None))
    due = or_(GenerationTask.dispatched_at.is_(None),
        GenerationTask.dispatched_at <= now - timedelta(seconds=DISPATCH_SECONDS))
    async with sessions() as db:
        candidates = list((await db.execute(select(GenerationTask.id, GenerationTask.project_id)
            .join(Project, Project.id == GenerationTask.project_id)
            .where(eligible, independent, due, Project.deleted_at.is_(None))
            .order_by(GenerationTask.dispatched_at.asc().nullsfirst(), GenerationTask.created_at, GenerationTask.id)
            .limit(limit).with_for_update(of=GenerationTask, skip_locked=True))).all())
    delivered = 0
    for candidate in candidates:
        try:
            async with sessions() as db:
                # Recheck the project first and the task second. Discovery
                # confers no ownership and never authorizes generation.
                project = await db.scalar(select(Project.id).where(Project.id == candidate.project_id,
                    Project.deleted_at.is_(None)).with_for_update(read=True, key_share=True, skip_locked=True))
                if project is None:
                    continue
                identity = await db.scalar(select(GenerationTask.id).where(GenerationTask.id == candidate.id,
                    GenerationTask.project_id == candidate.project_id, eligible, independent, due)
                    .with_for_update(skip_locked=True))
                if identity is None:
                    continue
                await db.execute(update(GenerationTask).where(GenerationTask.id == identity)
                    .values(dispatched_at=now, updated_at=GenerationTask.updated_at)
                    .execution_options(synchronize_session=False))
                await db.commit()
            # Publish only after a confirmed marker commit. A lost reply
            # retains the same ID and attempt budget for the next scan.
            await publish(str(identity))
            delivered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Independent task delivery unconfirmed for %s; original request retained', candidate.id)
    return delivered


async def publish_task(task_id):
    from app.tasks.generation_tasks import recover_async_generation
    await asyncio.to_thread(recover_async_generation.apply_async, args=[task_id], retry=False)


async def dispatch_pending_tasks():
    from app.db.session import async_session_factory
    return await dispatch_tasks(async_session_factory, publish_task)
