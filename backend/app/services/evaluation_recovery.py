"""Requeue stranded evaluation IDs without stealing a live worker's lock."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.project import EvaluateTask
from app.services.evaluation_lock import evaluation_worker_lock
from app.services.recovery_deadline import bounded_recovery


def utcnow():
    return datetime.now(timezone.utc)


async def _dispatch(task_id, chapter_id):
    from app.tasks.evaluation_tasks import dispatch_evaluate_task
    return await asyncio.to_thread(dispatch_evaluate_task, task_id, chapter_id, caller='evaluation.recovery')


async def claim_recovery_batch(sessions, *, cutoff, limit):
    """Rotate inspected tasks without changing worker activity or task state."""
    activity = func.coalesce(EvaluateTask.updated_at, EvaluateTask.created_at)
    checked = func.coalesce(EvaluateTask.recovery_checked_at, EvaluateTask.created_at)
    async with sessions() as db:
        rows = list((await db.scalars(select(EvaluateTask).where(
            EvaluateTask.status.in_(['pending', 'running']), activity <= cutoff,
            checked <= cutoff,
        ).order_by(checked, EvaluateTask.id)
            .limit(min(max(limit, 1), 100))
            .with_for_update(skip_locked=True))).all())
        ids = [row.id for row in rows]
        now = utcnow()
        for row in rows:
            row.recovery_checked_at = now
        await db.commit()
    return ids


@bounded_recovery
async def recover_evaluation_tasks(sessions, *, dispatch=None, limit=20):
    dispatch = dispatch or _dispatch
    cutoff = utcnow() - timedelta(seconds=60)
    ids = await claim_recovery_batch(sessions, cutoff=cutoff, limit=limit)
    counts = dict(examined=len(ids), enqueued=0, busy=0, skipped=0, failed=0)
    for task_id in ids:
        try:
            async with evaluation_worker_lock(task_id) as lease:
                if not lease:
                    counts['busy'] += 1
                    continue
                await lease.check()
                async with lease.session() as db:
                    row = await db.get(EvaluateTask, task_id)
                    last = (row.updated_at or row.created_at) if row is not None else None
                    if last is not None and last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if row is None or row.status not in ('pending', 'running') or last is None or last > cutoff:
                        counts['skipped'] += 1
                        continue
                    chapter_id = str(row.chapter_id)
                    # Reserve this dispatch window under the worker mutex.
                    # A lost/ambiguous broker reply remains eligible next minute.
                    row.updated_at = utcnow()
                    await db.commit()
            # Publish only after commit and lock release so a fast worker can run.
            async with asyncio.timeout(10):
                sent = await dispatch(str(task_id), chapter_id)
            counts['enqueued' if sent else 'failed'] += 1
        except Exception:
            counts['failed'] += 1
    return counts
