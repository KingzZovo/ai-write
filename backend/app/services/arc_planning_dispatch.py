"""Bounded redelivery of persisted arc request IDs, never a new paid request."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import and_, or_, select, update

from app.models.arc_planning import ArcPlanningRequest
from app.models.project import Project
from app.services.arc_planning_execution import STALE_SECONDS

logger = logging.getLogger(__name__)
DISPATCH_SECONDS = 30


async def dispatch_arc_requests(sessions, publish, *, limit=32, now=None):
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 100))
    model = ArcPlanningRequest
    eligible = or_(model.status == "pending", and_(model.status == "running", model.execution_id.is_not(None),
        model.heartbeat_at <= now - timedelta(seconds=STALE_SECONDS)))
    due = or_(model.dispatched_at.is_(None), model.dispatched_at <= now - timedelta(seconds=DISPATCH_SECONDS))
    async with sessions() as db:
        candidates = list((await db.execute(select(model.id, model.project_id).join(Project, Project.id == model.project_id)
            .where(eligible, due, Project.deleted_at.is_(None)).order_by(model.dispatched_at.asc().nullsfirst(),
                model.created_at, model.id).limit(limit).with_for_update(of=model, skip_locked=True))).all())
    delivered = 0
    for candidate in candidates:
        try:
            async with sessions() as db:
                project = await db.scalar(select(Project.id).where(Project.id == candidate.project_id,
                    Project.deleted_at.is_(None)).with_for_update(read=True, key_share=True, skip_locked=True))
                if project is None:
                    continue
                identity = await db.scalar(select(model.id).where(model.id == candidate.id,
                    model.project_id == candidate.project_id, eligible, due).with_for_update(skip_locked=True))
                if identity is None:
                    continue
                await db.execute(update(model).where(model.id == identity).values(dispatched_at=now,
                    updated_at=model.updated_at).execution_options(synchronize_session=False))
                await db.commit()
            await publish(str(identity))
            delivered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Arc planning dispatch unconfirmed for %s; original request retained", candidate.id)
    return delivered


async def publish_arc_request(request_id, *, manual=False):
    from app.tasks.arc_tasks import run_arc_planning
    await asyncio.to_thread(run_arc_planning.apply_async, args=[request_id], kwargs={"manual": manual}, retry=False)


async def dispatch_pending_arcs():
    from app.db.session import async_session_factory
    return await dispatch_arc_requests(async_session_factory, publish_arc_request)
