"""Persist and retry author intent; the caller owns the SQL transaction."""
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from app.models.project import ForeshadowReview as Review, ForeshadowConfirmationJob as Job, _utcnow


async def stage_confirmation(db, review, actor):
    await db.execute(insert(Job).values(id=review['id'], project_id=review['project_id'],
        requested_at=_utcnow(), requested_by=actor, status='pending', attempts=0, updated_at=_utcnow())
        .on_conflict_do_nothing(index_elements=['id']))
    await db.execute(update(Review).where(Review.id == review['id'], Review.status == 'pending')
        .values(status='confirming').execution_options(synchronize_session=False))
    return 'confirming'


async def retry_confirmation(db, project_id, review_id):
    from app.services.foreshadow_review_actions import ReviewSourceChanged, ReviewAlreadyHandled
    # Review before job, matching the worker; no story mutation on retries.
    status = (await db.execute(select(Review.status).where(Review.id == review_id,
        Review.project_id == project_id).with_for_update())).scalar_one_or_none()
    if status == 'accepted': return 'accepted'
    if status != 'confirming': raise ReviewAlreadyHandled()
    job = (await db.execute(select(Job.status).where(Job.id == review_id,
        Job.project_id == project_id).with_for_update())).scalar_one_or_none()
    if job == 'failed':
        await db.execute(update(Job).where(Job.id == review_id, Job.project_id == project_id)
            .values(status='pending', attempts=0, attempt_token=None, last_error=None, updated_at=_utcnow())
            .execution_options(synchronize_session=False))
    elif job not in ('pending', 'queued', 'running'):
        raise ReviewSourceChanged()
    return 'confirming'
