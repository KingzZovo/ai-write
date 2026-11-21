"""Transfer committed source notifications to durable extraction work.

No broker or model call is needed for this handoff. Existing extraction replay
owns delivery. A crash rolls back both the receipt and outbox acknowledgement.
"""
from sqlalchemy import select, delete, update
from sqlalchemy.dialects.postgresql import insert
from app.models.entity_extraction import ChapterEntityExtraction as Record, EntitySourceOutbox as Outbox
from app.models.project import Chapter, Volume
from app.services.entity_sources import resolve_entity_source
from app.services.entity_projection import now


async def drain_entity_source_outbox(sessions, *, limit=50):
    async with sessions() as db:
        rows = (await db.execute(select(Outbox.chapter_id, Outbox.revision)
            .order_by(Outbox.updated_at, Outbox.chapter_id)
            .limit(min(max(limit, 1), 50)))).all()
    transferred = 0
    for row in rows:
        async with sessions() as db:
            project_id = await db.scalar(select(Volume.project_id).join(Chapter, Chapter.volume_id == Volume.id)
                                         .where(Chapter.id == row.chapter_id))
            source = await resolve_entity_source(db, project_id, 0, row.chapter_id) if project_id else None
            if source is None:
                # Keep unresolved chronology/identity repairable, while rotating the
                # item behind other work. Deleted chapters cascade the delivery row.
                await db.execute(update(Outbox).where(Outbox.chapter_id == row.chapter_id,
                    Outbox.revision == row.revision).values(updated_at=now()))
            else:
                await db.execute(insert(Record).values(
                    project_id=source.project_id, chapter_id=source.chapter_id,
                    source_hash=source.source_hash, source_json=source.receipt, status='pending',
                ).on_conflict_do_nothing(constraint='uq_chapter_entity_source'))
                from app.services.cognition_extraction import enqueue_cognition
                await enqueue_cognition(db, source)
                from app.services.foreshadow_review_jobs import enqueue_review_job
                await enqueue_review_job(db, source, row.revision)
                # A concurrent author save increments revision. Never acknowledge
                # that newer notification with the source we read earlier.
                await db.execute(delete(Outbox).where(Outbox.chapter_id == row.chapter_id,
                                                       Outbox.revision == row.revision))
                transferred += 1
            await db.commit()
    return {'examined': len(rows), 'transferred': transferred}
