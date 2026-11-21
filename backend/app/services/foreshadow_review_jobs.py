"""Durable handoff of saved prose to clue discovery and payoff review work."""
from uuid import UUID, uuid4
from sqlalchemy.dialects.postgresql import insert
from app.models.project import ForeshadowReviewJob, _utcnow


async def enqueue_review_job(db, source, revision: int, *, rescan_discovery=False, job_id=None) -> None:
    """Share the source-outbox transaction; never acknowledge delivery here.

    Restoring the same prose creates a new revision and deserves a fresh
    review; repeating the same handoff must not restart completed work.
    """
    if type(revision) is not int or revision <= 0:
        raise ValueError('invalid_review_source_revision')
    await db.execute(insert(ForeshadowReviewJob).values(
        id=job_id or uuid4(), project_id=UUID(str(source.project_id)), chapter_id=UUID(str(source.chapter_id)),
        source_revision=revision, source_hash=source.source_hash, source_json=source.receipt,
        status='pending', attempts=0, rescan_discovery=rescan_discovery, created_at=_utcnow(), updated_at=_utcnow(),
    ).on_conflict_do_nothing(index_elements=['chapter_id', 'source_revision']))


class ReviewJobNotFound(Exception):
    pass


class ReviewJobNotRetryable(Exception):
    pass


async def list_review_jobs(db, project_id, limit=20, offset=0):
    from sqlalchemy import select, func
    from app.models.project import Chapter, Volume
    job = ForeshadowReviewJob
    ranked = select(job.id, job.chapter_id, job.status, job.attempts, job.updated_at, job.discovery_count,
        job.rescan_discovery, job.discovery_run_id,
        Chapter.title.label("chapter_title"), Chapter.global_idx,
        func.row_number().over(partition_by=job.chapter_id,
            order_by=(job.source_revision.desc(), job.id.desc())).label('position'))
    ranked = ranked.join(Chapter, job.chapter_id == Chapter.id).join(Volume, Chapter.volume_id == Volume.id)
    ranked = ranked.where(job.project_id == project_id, Volume.project_id == project_id).subquery()
    rows = list((await db.execute(select(ranked).where(ranked.c.position == 1)
        .order_by(ranked.c.updated_at.desc(), ranked.c.id.desc()).limit(limit + 1).offset(offset)
        .execution_options(autoflush=False))).mappings().all())
    # Do not expose provider exception strings or internal source receipts.
    return {'jobs': [dict(row) for row in rows[:limit]], 'has_more': len(rows) > limit}


async def retry_review_job(db, project_id, job_id):
    from sqlalchemy import select, update
    from app.services.entity_sources import resolve_entity_source
    from app.services.foreshadow_review_worker import latest_revision
    job = ForeshadowReviewJob
    query = select(*job.__table__.columns).where(job.project_id == project_id, job.id == job_id)
    query = query.execution_options(autoflush=False)
    row = (await db.execute(query)).mappings().one_or_none()
    if row is None:
        raise ReviewJobNotFound()
    if row['status'] != 'failed':
        raise ReviewJobNotRetryable('该任务正在处理或无需重试，请刷新查看')
    # Same source-before-job lock order as worker completion.
    source = await resolve_entity_source(db, project_id, 0, row['chapter_id'], lock=True, allow_review_drafts=True)
    current = (await db.execute(query.with_for_update())).mappings().one_or_none()
    if current is None:
        raise ReviewJobNotFound()
    if current['status'] != 'failed':
        raise ReviewJobNotRetryable('该任务状态已变化，请刷新查看')
    if (source is None or source.source_hash != current['source_hash']
            or not await latest_revision(db, current)):
        raise ReviewJobNotRetryable('正文来源已变化，请等待最新版本的核查任务')
    await db.execute(update(job).where(job.id == job_id, job.project_id == project_id)
        .values(status='pending', attempts=0, attempt_token=None, last_error=None, discovery_count=None, updated_at=_utcnow())
        .execution_options(autoflush=False, synchronize_session=False))


class ReviewSourceNotReady(Exception):
    pass


async def create_review_job(db, project_id, chapter_id, expected_content, *, request_id=None, rescan_discovery=False):
    """Lock the saved chapter and enqueue a review without promoting draft facts."""
    from sqlalchemy import select
    from app.models.entity_extraction import entity_source_outbox_revisions
    from app.models.project import Project
    from app.services.entity_sources import resolve_entity_source
    from app.services.foreshadow_review_worker import latest_revision
    from app.services.foreshadow_review_requests import reserve_request, bind_request_job, request_result
    if type(rescan_discovery) is not bool or (rescan_discovery and request_id is None):
        raise ReviewSourceNotReady('重新识别线索需要固定请求编号')
    project_id, chapter_id = UUID(str(project_id)), UUID(str(chapter_id))
    receipt = None
    if request_id is not None:
        receipt, fresh = await reserve_request(db, project_id, chapter_id, request_id, expected_content, rescan_discovery)
        if not fresh:
            # Read the committed original outcome without requiring a still
            # live chapter, and without allocating another inference cycle.
            return await request_result(db, receipt)
    project = (await db.execute(select(Project.id).where(Project.id == project_id, Project.deleted_at.is_(None))
        .with_for_update(read=True).execution_options(autoflush=False))).scalar_one_or_none()
    if project is None:
        raise ReviewSourceNotReady('作品已不可编辑，请恢复作品后再核查')
    source = await resolve_entity_source(db, project_id, 0, chapter_id,
        lock=True, allow_review_drafts=True)
    if source is None:
        raise ReviewSourceNotReady('章节不可核查，请确认正文已保存、生成已结束且全书章号有效')
    if source.content != expected_content:
        raise ReviewSourceNotReady('编辑区与已保存正文不一致，请先保存并重新核对')
    job = ForeshadowReviewJob
    # Chapter lock serializes manual submissions; the worker owns job status.
    row = (await db.execute(select(*job.__table__.columns).where(
        job.project_id == project_id, job.chapter_id == chapter_id)
        .order_by(job.source_revision.desc()).limit(1).with_for_update()
        .execution_options(autoflush=False))).mappings().one_or_none()
    if (row is not None and row['status'] in {'pending', 'queued', 'running'}
            and row['source_hash'] == source.source_hash and await latest_revision(db, row)):
        if rescan_discovery and not row['rescan_discovery']:
            raise ReviewSourceNotReady('本章已有普通核查正在进行，请结束后再重新识别线索')
        result = dict(id=str(row['id']), chapter_id=str(chapter_id), status=row['status'],
            rescan_discovery=row['rescan_discovery'])
    else:
        revision = (await db.execute(select(entity_source_outbox_revisions.next_value()))).scalar_one()
        await enqueue_review_job(db, source, revision, rescan_discovery=rescan_discovery,
            job_id=receipt['job_id'] if receipt else None)
        created = (await db.execute(select(job.id).where(job.chapter_id == chapter_id,
            job.source_revision == revision))).scalar_one()
        result = dict(id=str(created), chapter_id=str(chapter_id), status='pending', rescan_discovery=rescan_discovery)
    if receipt is not None:
        await bind_request_job(db, receipt, result['id'])
        result['request_id'] = str(receipt['id'])
    return result


async def enqueue_saved_draft_review(project_id, volume_id, chapter_idx, expected_content, *, sessions=None):
    """Batch drafts share manual review jobs; completed prose uses its outbox.

    Own a short transaction so a borrowed generation session is never committed
    here. The job source resolver checks the exact body and persisted position.
    """
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import Chapter, Volume
    sessions = sessions or async_session_factory
    project_id, volume_id = UUID(str(project_id)), UUID(str(volume_id))
    async with sessions() as db:
        rows = (await db.execute(select(Chapter.id, Chapter.status)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == project_id, Chapter.volume_id == volume_id,
                   Chapter.chapter_idx == chapter_idx).limit(2)
            .execution_options(autoflush=False))).all()
        if len(rows) != 1:
            raise ReviewSourceNotReady('章节来源不存在或不唯一')
        if rows[0].status == 'completed':
            return None
        result = await create_review_job(db, project_id, rows[0].id, expected_content)
        await db.commit()
        return result
