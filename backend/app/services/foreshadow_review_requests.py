"""Bind manual review retries to one committed intent, even after source deletion."""
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.project import ForeshadowReviewRequest as Receipt, ForeshadowReviewJob as Job, _utcnow
from app.services.entity_sources import digest


class ReviewRequestConflict(Exception):
    pass


async def reserve_request(db, project_id, chapter_id, request_id, content, rescan):
    """Caller owns the transaction. Acquire this reservation before source locks.

    An uncommitted placeholder job ID is replaced when coalescing active work.
    No incomplete receipt can be observed or acknowledged outside this txn.
    """
    request_id = UUID(str(request_id))
    signature = digest(dict(version=1, project_id=str(project_id), chapter_id=str(chapter_id),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(), rescan_discovery=rescan))
    inserted = (await db.execute(insert(Receipt).values(id=request_id, project_id=project_id,
        chapter_id=chapter_id, request_sha256=signature, job_id=uuid4(), rescan_discovery=rescan,
        created_at=_utcnow()).on_conflict_do_nothing(index_elements=['id']).returning(Receipt.id)
        .execution_options(autoflush=False))).scalar_one_or_none()
    row = (await db.execute(select(*Receipt.__table__.columns).where(Receipt.id == request_id)
        .with_for_update().execution_options(autoflush=False))).mappings().one()
    if (row['project_id'] != project_id or row['chapter_id'] != chapter_id
            or row['request_sha256'] != signature or row['rescan_discovery'] != rescan):
        raise ReviewRequestConflict('该请求编号已用于另一份核查请求，请刷新后重新提交')
    return row, inserted is not None


async def bind_request_job(db, receipt, job_id):
    await db.execute(update(Receipt).where(Receipt.id == receipt['id'])
        .values(job_id=UUID(str(job_id))).execution_options(autoflush=False, synchronize_session=False))


async def request_result(db, receipt):
    job = (await db.execute(select(Job.status, Job.rescan_discovery).where(Job.id == receipt['job_id'],
        Job.project_id == receipt['project_id'], Job.chapter_id == receipt['chapter_id'])
        .execution_options(autoflush=False))).mappings().one_or_none()
    return dict(id=str(receipt['job_id']), chapter_id=str(receipt['chapter_id']),
        request_id=str(receipt['id']), status=job['status'] if job else 'unavailable',
        rescan_discovery=job['rescan_discovery'] if job else receipt['rescan_discovery'])
