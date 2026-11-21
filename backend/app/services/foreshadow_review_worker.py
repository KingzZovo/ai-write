"""Source-checked, attempt-fenced clue discovery and payoff review jobs."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4
from sqlalchemy import select, update, func, or_, and_
from app.db.session import async_session_factory
from app.models.project import ForeshadowReviewJob as Job, ForeshadowDiscoveryRun as Run, _utcnow
from app.models.entity_extraction import EntitySourceOutbox as Outbox
from app.services.entity_sources import resolve_entity_source
from app.services.foreshadow_manager import ForeshadowManager
from app.services.foreshadow_review import review_scoped_chapter
from app.services.foreshadow_review_store import persist_resolution_review
from app.services.foreshadow_discovery import cached_discovery, detect_clues, persist_discovery

MAX_ATTEMPTS = 3
RECOVERY_DELAY = timedelta(minutes=10)


def due_jobs():
    return and_(Job.attempts < MAX_ATTEMPTS, or_(Job.status == 'pending', and_(
        Job.status.in_(['failed', 'queued', 'running']), Job.updated_at < _utcnow() - RECOVERY_DELAY)))


async def latest_revision(db, row):
    job_revision = (await db.execute(select(func.max(Job.source_revision)).where(
        Job.chapter_id == row['chapter_id']))).scalar_one_or_none()
    outbox_revision = (await db.execute(select(Outbox.revision).where(
        Outbox.chapter_id == row['chapter_id']))).scalar_one_or_none()
    return max(job_revision or 0, outbox_revision or 0) <= row['source_revision']


async def run_review_job(job_id, *, sessions=async_session_factory):
    job_id, token = UUID(str(job_id)), uuid4()
    async with sessions() as db:
        row = (await db.execute(update(Job).where(Job.id == job_id, Job.attempts < MAX_ATTEMPTS,
            or_(Job.status.in_(['pending', 'queued', 'failed']), and_(Job.status == 'running',
                Job.updated_at < _utcnow() - RECOVERY_DELAY)))
            .values(status='running', attempt_token=token, attempts=Job.attempts + 1,
                last_error=None, updated_at=_utcnow()).returning(*Job.__table__.columns)
            .execution_options(synchronize_session=False))).mappings().one_or_none()
        await db.commit()
    if row is None:
        return {'status': 'not_claimed'}

    async def finish(status, error=None):
        async with sessions() as db:
            changed = (await db.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token,
                Job.status == 'running').values(status=status, last_error=error, updated_at=_utcnow())
                .returning(Job.id).execution_options(synchronize_session=False))).scalar_one_or_none()
            await db.commit()
        return {'status': status if changed else 'superseded'}

    try:
        async with asyncio.timeout(300):
            async with sessions() as db:
                source = await resolve_entity_source(db, row['project_id'], 0, row['chapter_id'], allow_review_drafts=True)
                if source is None or source.source_hash != row['source_hash'] or not await latest_revision(db, row):
                    source = None
                else:
                    discovery = await cached_discovery(db, source,
                        scan_revision=row['source_revision'] if row['rescan_discovery'] else None)
            review = None
            if source is not None:
                # Release the read transaction before new model IO. A cached
                # full-prose result (even empty) is reused by ordinary jobs.
                # An explicit rescan only reuses its own committed scan.
                if discovery is None:
                    discovery = await detect_clues(source)
                    if row['rescan_discovery']:
                        discovery = replace(discovery, scan_revision=row['source_revision'])
                async with sessions() as db:
                    review = await review_scoped_chapter(db=db, manager=ForeshadowManager(db),
                        project_id=str(row['project_id']), volume_id=source.receipt['volume_id'],
                        chapter_idx=source.receipt['chapter_idx'], expected_text=source.content)
        if review is None:
            return await finish('obsolete')
        async with sessions() as db:
            current = await resolve_entity_source(db, row['project_id'], 0, row['chapter_id'], lock=True, allow_review_drafts=True)
            live = (await db.execute(select(Job.id).where(Job.id == job_id, Job.attempt_token == token,
                Job.status == 'running').with_for_update())).scalar_one_or_none()
            if live is None:
                return {'status': 'superseded'}
            if current != source or not await latest_revision(db, row):
                await db.execute(update(Job).where(Job.id == job_id).values(status='obsolete', updated_at=_utcnow()))
                await db.commit()
                return {'status': 'obsolete'}
            saved = await persist_resolution_review(db, review, strict_source=True)
            discovered = await persist_discovery(db, discovery, locked_source=current)
            run_id = (await db.execute(select(Run.id).where(Run.project_id == row['project_id'],
                Run.chapter_id == row['chapter_id'], Run.source_sha256 == discovery.source_sha256,
                Run.scan_revision == discovery.scan_revision))).scalar_one()
            await db.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token)
                .values(status='completed', discovery_count=discovered, discovery_run_id=run_id,
                    last_error=None, updated_at=_utcnow()))
            await db.commit()
        return {'status': 'completed', 'saved': saved, 'discovered': discovered}
    except (Exception, asyncio.CancelledError) as exc:
        await finish('failed', type(exc).__name__[:128])
        raise


async def replay_review_jobs(sessions=async_session_factory, *, dispatch=None, limit=20):
    if dispatch is None:
        from app.tasks.foreshadow_tasks import review_chapter
        dispatch = lambda job_id: review_chapter.delay(job_id)
    async with sessions() as db:
        await db.execute(update(Job).where(Job.attempts >= MAX_ATTEMPTS,
            Job.status.in_(['queued', 'running']), Job.updated_at < _utcnow() - RECOVERY_DELAY)
            .values(status='failed', attempt_token=None, last_error='attempts_exhausted', updated_at=_utcnow()))
        await db.commit()
        ids = (await db.execute(select(Job.id).where(due_jobs()).order_by(Job.updated_at, Job.id)
            .limit(min(max(limit, 1), 20)))).scalars().all()
    sent = 0
    for job_id in ids:
        async with sessions() as db:
            claimed = (await db.execute(update(Job).where(Job.id == job_id, due_jobs())
                .values(status='queued', updated_at=_utcnow()).returning(Job.id))).scalar_one_or_none()
            await db.commit()
        if claimed is None:
            continue
        try:
            dispatch(str(job_id))
            sent += 1
        except Exception:
            # Keep the queued lease: recovery will retry after the delay,
            # without a tight dispatch loop or overwriting a running worker.
            continue
    return {'examined': len(ids), 'sent': sent}
