"""Author actions on review records, separate from underlying story facts."""
from uuid import UUID
from sqlalchemy import select, update
from app.models.project import ForeshadowReview, _utcnow


class ReviewNotFound(Exception):
    pass


class ReviewAlreadyHandled(Exception):
    pass


async def dismiss_review(db, project_id: UUID, review_id: UUID, *, actor: str | None = None) -> None:
    """Atomically dismiss pending records; retries preserve prior decisions.

    Dismissal intentionally needs no current-source check: an obsolete or
    incorrect suggestion can be ignored without editing the story's facts.
    The caller's transaction commits or rolls back this single-table change.
    """
    from sqlalchemy import exists, and_, or_
    from app.models.project import ForeshadowConfirmationJob as Job
    dismissible = or_(ForeshadowReview.status == 'pending', and_(
        ForeshadowReview.status == 'confirming', exists(select(Job.id).where(
            Job.id == ForeshadowReview.id, Job.project_id == project_id, Job.status == 'rejected'))))
    changed = (await db.execute(update(ForeshadowReview).where(
        ForeshadowReview.project_id == project_id, ForeshadowReview.id == review_id,
        dismissible,
    ).values(status='dismissed', handled_at=_utcnow(), handled_by=actor).returning(ForeshadowReview.id)
      .execution_options(autoflush=False, synchronize_session=False))).scalar_one_or_none()
    if changed is not None:
        return
    status = (await db.execute(select(ForeshadowReview.status).where(
        ForeshadowReview.project_id == project_id, ForeshadowReview.id == review_id,
    ).execution_options(autoflush=False))).scalar_one_or_none()
    if status is None:
        raise ReviewNotFound()
    if status != 'dismissed':
        raise ReviewAlreadyHandled()


class ReviewSourceChanged(Exception):
    pass


class ReviewSourceRequiresSync(Exception):
    pass


async def accept_review(db, project_id: UUID, review_id: UUID, *, actor: str | None = None) -> str:
    """Confirm a PG-owned payoff or stage a durable graph confirmation intent.

    Lock order matches review creation: chapter/volume, foreshadow, review.
    No model or graph IO happens while those locks are held.
    """
    from app.models.project import Chapter, Volume, Foreshadow
    from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, REVIEWABLE_STATUSES
    from app.services.foreshadow_review_reader import review_source_state

    review_query = select(*ForeshadowReview.__table__.columns).where(
        ForeshadowReview.project_id == project_id, ForeshadowReview.id == review_id,
    ).execution_options(autoflush=False)
    initial = (await db.execute(review_query)).mappings().one_or_none()
    if initial is None:
        raise ReviewNotFound()
    if initial['status'] == 'accepted':
        return 'accepted'  # Retry acknowledges the recorded decision; it never replays it.
    if initial['status'] == 'confirming':
        from app.services.foreshadow_confirmation_jobs import retry_confirmation
        return await retry_confirmation(db, project_id, review_id)
    if initial['status'] != 'pending':
        raise ReviewAlreadyHandled()
    chapter = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Chapter.content_text, Chapter.updated_at, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id, Chapter.id == initial['chapter_id'])
        .with_for_update(of=(Chapter, Volume)).execution_options(autoflush=False))).mappings().one_or_none()
    foreshadow = (await db.execute(select(Foreshadow.id, *(getattr(Foreshadow, k) for k in REVIEW_SOURCE_FIELDS))
        .where(Foreshadow.project_id == project_id, Foreshadow.id == initial['foreshadow_id'])
        .with_for_update().execution_options(autoflush=False))).mappings().one_or_none()
    review = (await db.execute(review_query.with_for_update())).mappings().one_or_none()
    if review is None:
        raise ReviewNotFound()
    if review['status'] == 'accepted':
        return 'accepted'
    if review['status'] == 'confirming':
        from app.services.foreshadow_confirmation_jobs import retry_confirmation
        return await retry_confirmation(db, project_id, review_id)
    if review['status'] != 'pending':
        raise ReviewAlreadyHandled()
    if (review['chapter_id'] != initial['chapter_id'] or review['foreshadow_id'] != initial['foreshadow_id']):
        raise ReviewSourceChanged()
    state = review_source_state(review, chapter, foreshadow)
    if state == 'projection_current':
        if foreshadow['source'] != 'neo4j':
            raise ReviewSourceRequiresSync()
        if foreshadow['status'] not in REVIEWABLE_STATUSES:
            raise ReviewSourceChanged()
        from app.services.foreshadow_confirmation_jobs import stage_confirmation
        return await stage_confirmation(db, review, actor)
    if state != 'current' or foreshadow['status'] not in REVIEWABLE_STATUSES:
        raise ReviewSourceChanged()
    await db.execute(update(Foreshadow).where(Foreshadow.id == foreshadow['id'],
        Foreshadow.project_id == project_id).values(status='resolved',
        resolved_chapter=chapter['global_idx'], narrative_proximity=1.0)
        .execution_options(autoflush=False, synchronize_session=False))
    await db.execute(update(ForeshadowReview).where(ForeshadowReview.id == review_id,
        ForeshadowReview.project_id == project_id).values(status='accepted', handled_at=_utcnow(), handled_by=actor)
        .execution_options(autoflush=False, synchronize_session=False))
    return 'accepted'
