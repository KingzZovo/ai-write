"""Recover graph confirmations without replaying an author's already applied edit."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from sqlalchemy import select, update, or_, and_

from app.db.session import async_session_factory
from app.models.project import Chapter, Volume, Foreshadow, ForeshadowReview as Review, ForeshadowConfirmationJob as Job, _utcnow
from app.services.entity_materialization_lock import require_materialization_lock
from app.services.foreshadow_graph_confirmation import decide_graph_confirmation
from app.services.foreshadow_projection import project_current_foreshadow
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS
from app.services.foreshadow_review_reader import review_source_state

MAX_ATTEMPTS = 3
DELAY = timedelta(minutes=10)


def due():
    return and_(Job.attempts < MAX_ATTEMPTS, or_(Job.status == 'pending', and_(
        Job.status.in_(['queued', 'running', 'failed']), Job.updated_at < _utcnow() - DELAY)))


async def run_confirmation(job_id, *, driver, sessions=async_session_factory):
    job_id, token = UUID(str(job_id)), uuid4()
    async with sessions() as db:
        claimed = (await db.execute(update(Job).where(Job.id == job_id, Job.attempts < MAX_ATTEMPTS,
            or_(Job.status.in_(['pending', 'queued', 'failed']), and_(Job.status == 'running',
                Job.updated_at < _utcnow() - DELAY)))
            .values(status='running', attempt_token=token, attempts=Job.attempts + 1, updated_at=_utcnow(), last_error=None)
            .returning(Job.project_id).execution_options(synchronize_session=False))).scalar_one_or_none()
        await db.commit()
    if claimed is None: return {'status': 'not_claimed'}
    try:
        async with asyncio.timeout(90):
            async with sessions() as db:
                await require_materialization_lock(db, claimed)
                initial = (await db.execute(select(*Review.__table__.columns).where(
                    Review.id == job_id, Review.project_id == claimed))).mappings().one_or_none()
                if initial is None: return {'status': 'superseded'}
                chapter = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
                    Chapter.global_idx, Chapter.content_text, Chapter.updated_at, Chapter.status)
                    .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == claimed,
                        Chapter.id == initial['chapter_id']).with_for_update(of=(Chapter, Volume))
                    .execution_options(autoflush=False))).mappings().one_or_none()
                fs = (await db.execute(select(Foreshadow.id, *(getattr(Foreshadow, k) for k in REVIEW_SOURCE_FIELDS))
                    .where(Foreshadow.project_id == claimed, Foreshadow.id == initial['foreshadow_id'])
                    .with_for_update().execution_options(autoflush=False))).mappings().one_or_none()
                review = (await db.execute(select(*Review.__table__.columns).where(Review.id == job_id,
                    Review.project_id == claimed).with_for_update())).mappings().one_or_none()
                job = (await db.execute(select(*Job.__table__.columns).where(Job.id == job_id,
                    Job.project_id == claimed, Job.status == 'running', Job.attempt_token == token)
                    .with_for_update())).mappings().one_or_none()
                if job is None:
                    # 被别的执行者接管(attempt_token 已被替换):什么都不写。
                    return {'status': 'superseded'}
                if review is None or review['status'] != 'confirming':
                    # 2026-11-17:这里原先直接 return,而认领时已经把 job 置为 running、
                    # attempts+1 —— 于是任务卡在 running,每 10 分钟被重投,
                    # 3 次后由重试上限记成 failed,真实原因(评审已被丢弃/已确认)丢失。
                    # 既然 token 还属于本轮,就如实收尾。
                    await db.execute(update(Job).where(Job.id == job_id,
                        Job.status == 'running', Job.attempt_token == token,
                    ).values(status='superseded', updated_at=_utcnow(),
                             last_error='review_not_confirming').execution_options(synchronize_session=False))
                    await db.commit()
                    return {'status': 'superseded'}
                snapshot = review['source_snapshot']
                allow_apply = review_source_state(review, chapter, fs) == 'projection_current'
                receipt = await decide_graph_confirmation(driver, review_id=job_id, project_id=claimed,
                    foreshadow_id=review['foreshadow_id'], snapshot_sha256=review['snapshot_sha256'],
                    expected=snapshot['foreshadow'], global_idx=snapshot['global_idx'], allow_apply=allow_apply)
                if receipt['outcome'] == 'applied':
                    # Read the graph now, under the same projection mutex. A later
                    # author edit must never be replaced by the receipt's old value.
                    await project_current_foreshadow(db, driver, claimed, review['foreshadow_id'])
                    await db.execute(update(Review).where(Review.id == job_id, Review.status == 'confirming')
                        .values(status='accepted', handled_by=job['requested_by'],
                            handled_at=datetime.fromtimestamp(receipt['decided_at'] / 1000, tz=timezone.utc))
                        .execution_options(synchronize_session=False))
                    outcome = 'completed'
                elif receipt['outcome'] == 'rejected':
                    outcome = 'rejected'
                else:
                    raise ValueError('invalid_confirmation_outcome')
                proof = {key: receipt[key] for key in ('id', 'project_id', 'foreshadow_id', 'snapshot_sha256', 'outcome', 'decided_at')}
                await db.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token)
                    .values(status=outcome, result_json=proof, last_error=None, updated_at=_utcnow())
                    .execution_options(synchronize_session=False))
                await db.commit()
                return {'status': outcome}
    except (Exception, asyncio.CancelledError) as exc:
        async with sessions() as db:
            await db.execute(update(Job).where(Job.id == job_id, Job.attempt_token == token, Job.status == 'running')
                .values(status='failed', last_error=type(exc).__name__[:128], updated_at=_utcnow())
                .execution_options(synchronize_session=False))
            await db.commit()
        raise


async def replay_confirmations(sessions=async_session_factory, *, dispatch=None, limit=20):
    if dispatch is None:
        from app.tasks.foreshadow_tasks import confirm_review
        dispatch = lambda job_id: confirm_review.delay(job_id)
    async with sessions() as db:
        await db.execute(update(Job).where(Job.attempts >= MAX_ATTEMPTS, Job.status.in_(['queued', 'running']),
            Job.updated_at < _utcnow() - DELAY).values(status='failed', attempt_token=None,
                last_error='attempts_exhausted', updated_at=_utcnow()))
        await db.commit()
        ids = (await db.execute(select(Job.id).where(due()).order_by(Job.updated_at, Job.id)
            .limit(min(max(limit, 1), 20)))).scalars().all()
    sent = 0
    for job_id in ids:
        async with sessions() as db:
            queued = (await db.execute(update(Job).where(Job.id == job_id, due())
                .values(status='queued', updated_at=_utcnow()).returning(Job.id))).scalar_one_or_none()
            await db.commit()
        if queued is None: continue
        try: dispatch(str(job_id)); sent += 1
        except Exception: pass  # The queue lease remains for recovery.
    return {'examined': len(ids), 'sent': sent}
