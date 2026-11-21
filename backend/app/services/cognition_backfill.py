"""Durable, bounded handoff of every completed chapter in an author snapshot."""
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.models.cognition import CognitionBackfill as Backfill
from app.models.project import Chapter, Volume, Project
from app.services.cognition_reader import load_source_records, load_revisions, record_state, require_project
from app.services.cognition_requests import reserve_request, acknowledge_request
from app.services.entity_sources import resolve_entity_sources
from app.services.entity_projection import now

BACKFILL_BATCH_SIZE = 100


async def create_backfill(db, project_id, request_id):
    """Snapshot IDs only. Model work remains in the ordinary extraction queue."""
    project_id = UUID(str(project_id))
    repeated = await reserve_request(db, project_id, request_id, dict(operation='backfill'))
    if repeated is not None:
        return repeated
    active = (await db.execute(select(Backfill.id, Backfill.chapter_count).where(
        Backfill.project_id == project_id, Backfill.status == 'pending')
        .execution_options(autoflush=False))).one_or_none()
    if active is None:
        chapter_ids = [str(cid) for cid in (await db.execute(select(Chapter.id)
            .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == project_id,
                Chapter.status == 'completed')
            .order_by(Volume.volume_idx, Volume.id, Chapter.chapter_idx, Chapter.id)
            .execution_options(autoflush=False))).scalars().all()]
        created = (await db.execute(insert(Backfill).values(id=uuid4(), project_id=project_id,
            chapter_ids=chapter_ids, chapter_count=len(chapter_ids),
            status='pending' if chapter_ids else 'scheduled')
            .on_conflict_do_update(index_elements=['project_id'], index_where=text("status = 'pending'"),
                set_={'updated_at': Backfill.updated_at})
            .returning(Backfill.id, Backfill.chapter_count).execution_options(autoflush=False))).one()
        active = created
    return await acknowledge_request(db, request_id, dict(operation='backfill', outcome='scheduled',
        backfill_id=str(active.id), chapter_count=active.chapter_count))


async def advance_backfills(sessions, *, batch_size=BACKFILL_BATCH_SIZE):
    """One short transaction per tick. A crash cannot advance only the cursor."""
    from app.services.cognition_extraction import enqueue_cognition
    async with sessions() as db:
        # Project deletion cascades to this job. Acquire the parent first,
        # before either a job lock or a child source lock can be held.
        job_id = (await db.execute(select(Backfill.id).join(Project, Backfill.project_id == Project.id)
            .where(Backfill.status == 'pending', Project.deleted_at.is_(None))
            .order_by(Backfill.updated_at, Backfill.id).limit(1)
            .with_for_update(of=Project, read=True, key_share=True, skip_locked=True)
            .execution_options(autoflush=False))).scalar_one_or_none()
        if job_id is None:
            return 0
        job = (await db.execute(select(Backfill).where(Backfill.id == job_id, Backfill.status == 'pending')
            .order_by(Backfill.updated_at, Backfill.id).limit(1).with_for_update(skip_locked=True)
            .execution_options(autoflush=False, populate_existing=True))).scalar_one_or_none()
        if job is None:
            return 0
        if (not isinstance(job.chapter_ids, list) or job.chapter_count != len(job.chapter_ids)
                or not 0 <= job.cursor <= job.chapter_count):
            job.status = 'failed'
            job.updated_at = now()
            await db.commit()
            return 0
        ids = job.chapter_ids[job.cursor:job.cursor + min(max(batch_size, 1), BACKFILL_BATCH_SIZE)]
        try:
            if any(str(UUID(cid)) != cid for cid in ids):
                raise ValueError('invalid_chapter_identity')
        except (ValueError, TypeError, AttributeError):
            job.status = 'failed'
            job.updated_at = now()
            await db.commit()
            return 0
        sources = await resolve_entity_sources(db, job.project_id, ids, lock=True)
        records = await load_source_records(db, job.project_id, sources, lock=True)
        revisions = await load_revisions(db, job.project_id, records)
        for cid in ids:
            source, record = sources.get(cid), records.get(cid)
            state = record_state(record, source, revisions.get(record.author_revision_id) if record else None)
            if state == 'missing':
                await enqueue_cognition(db, source)
                job.added_count += 1
            elif state == 'unavailable':
                job.unavailable_count += 1
            elif state in {'invalid', 'failed', 'obsolete'}:
                job.attention_count += 1
            else:
                job.existing_count += 1
        job.cursor += len(ids)
        job.status = 'scheduled' if job.cursor == job.chapter_count else 'pending'
        job.updated_at = now()
        await db.commit()
        return len(ids)


async def list_backfills(db, project_id):
    await require_project(db, project_id)
    rows = (await db.execute(select(Backfill.id, Backfill.status, Backfill.chapter_count, Backfill.cursor,
        Backfill.added_count, Backfill.existing_count, Backfill.attention_count, Backfill.unavailable_count,
        Backfill.updated_at).where(Backfill.project_id == UUID(str(project_id)))
        .order_by(Backfill.created_at.desc(), Backfill.id.desc()).limit(5)
        .execution_options(autoflush=False))).mappings().all()
    return dict(backfills=[dict(row) for row in rows])
