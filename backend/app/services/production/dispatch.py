"""Recover and deliver durable queued commands, without relying on broker state."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID
import asyncio

from sqlalchemy import or_, select

from app.models.pipeline import PipelineRun
from app.services.production.control import recover_abandoned

Publisher = Callable[[str, str], Awaitable[None]]
DISPATCH_RETRY_SECONDS = 30


async def dispatch_pending(sessions, publish: Publisher, *, limit: int = 100, pipeline_id=None) -> int:
    scope = [] if pipeline_id is None else [PipelineRun.id == UUID(str(pipeline_id))]
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        stale_ids = list((await db.scalars(select(PipelineRun.id).where(
            *scope, PipelineRun.state.in_(("generating", "pausing")),
            or_(PipelineRun.heartbeat_at.is_(None), PipelineRun.heartbeat_at < now - timedelta(seconds=90)),
        ).order_by(PipelineRun.updated_at).limit(limit))).all())
    for pipeline_id in stale_ids:
        async with sessions() as db:
            await recover_abandoned(db, pipeline_id)
            await db.commit()

    cutoff = now - timedelta(seconds=DISPATCH_RETRY_SECONDS)
    due = or_(PipelineRun.dispatched_at.is_(None), PipelineRun.dispatched_at <= cutoff)
    async with sessions() as db:
        candidates = list((await db.scalars(select(PipelineRun.id).where(
            *scope, PipelineRun.state == "queued", due,
        ).order_by(PipelineRun.queued_at).limit(limit))).all())
    delivered = 0
    for pipeline_id in candidates:
        async with sessions() as db:
            pipeline = await db.scalar(select(PipelineRun).where(
                PipelineRun.id == pipeline_id, PipelineRun.state == "queued", due,
            ).with_for_update(skip_locked=True))
            if pipeline is None or pipeline.command_id is None:
                continue
            command_id = str(pipeline.command_id)
            pipeline.dispatched_at = now
            await db.commit()
        # Commit first. A crash before/after publish is recoverable: a queued
        # row is delivered again after the bounded interval with the SAME token.
        try:
            await publish(str(pipeline_id), command_id)
            delivered += 1
        except Exception:
            async with sessions() as db:
                pipeline = await db.scalar(select(PipelineRun).where(
                    PipelineRun.id == pipeline_id,
                    PipelineRun.command_id == command_id, PipelineRun.state == "queued",
                ).with_for_update())
                if pipeline is not None:
                    pipeline.error_message = "任务队列暂时不可用，任务已保存，后台会自动重试"
                    await db.commit()
    return delivered


async def publish_command(pipeline_id: str, command_id: str) -> None:
    from app.tasks.generation_tasks import run_pipeline_generation
    await asyncio.to_thread(
        run_pipeline_generation.apply_async, args=[pipeline_id, command_id], retry=False,
    )


async def dispatch_queued_commands(pipeline_id=None) -> int:
    from app.db.session import async_session_factory
    return await dispatch_pending(async_session_factory, publish_command, pipeline_id=pipeline_id)
