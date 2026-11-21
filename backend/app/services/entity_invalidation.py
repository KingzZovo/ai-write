"""Replayable cleanup of source-owned facts after author mutations/deletions."""
import logging
from sqlalchemy import select, delete, update
from app.models.entity_extraction import EntityInvalidation as Pending, entity_projection_tokens
from app.models.project import Chapter, Volume, Project, CharacterMemoryCard
from app.services.entity_sources import resolve_entity_source
from app.services.entity_projection import entity_project_lock, EntityProjectionBusy, now
from app.services.entity_graph_projection import invalidate_entity_projection

logger = logging.getLogger(__name__)


async def invalidate_one(*, row, sessions, engine, driver, materialize):
    pid, cid = str(row.project_id), str(row.chapter_id)
    async with entity_project_lock(engine, pid) as lease:
        if not lease:
            raise EntityProjectionBusy('another entity worker owns this project')
        async with sessions() as db:
            # Lock existing author rows even if draft or moved to another project.
            # Hold these only across graph/projection work, never model IO.
            await db.execute(select(Chapter.id, Volume.id).join(Volume, Chapter.volume_id == Volume.id)
                             .where(Chapter.id == cid).with_for_update())
            source = await resolve_entity_source(db, pid, 0, cid)
            current_hash = source.source_hash if source else None
            project_exists = await db.scalar(select(Project.id).where(Project.id == pid))
            token = await db.scalar(entity_projection_tokens.next_value())
            await lease.check()
            changed = await invalidate_entity_projection(driver, project_id=pid, chapter_id=cid,
                                                         current_hash=current_hash, token=token)
            await lease.check()
            if project_exists:
                # Always retry read projections: a prior attempt may have committed
                # Neo4j and then failed before PG materialization completed.
                await materialize(project_id=pid, chapter_idx=source.global_idx if source else 0,
                                  caller='entity_invalidation', strict=True, graph_driver=driver)
            cards = delete(CharacterMemoryCard).where(CharacterMemoryCard.project_id == pid,
                                                       CharacterMemoryCard.source_chapter_id == cid)
            if current_hash:
                cards = cards.where(CharacterMemoryCard.source_hash.is_distinct_from(current_hash))
            await db.execute(cards)
            await lease.check()
            await db.commit()
        # Release author locks before touching the trigger-written queue row.
        # A newer author mutation changes revision, and cannot be acknowledged here.
        await lease.check()
        async with sessions() as db:
            await db.execute(delete(Pending).where(Pending.project_id == row.project_id,
                Pending.chapter_id == row.chapter_id, Pending.revision == row.revision))
            await db.commit()
    return changed


async def replay_entity_invalidations(*, sessions, engine, driver, materialize, limit=50):
    async with sessions() as db:
        rows = (await db.execute(select(Pending.project_id, Pending.chapter_id, Pending.revision)
            .order_by(Pending.updated_at, Pending.project_id, Pending.chapter_id)
            .limit(min(max(limit, 1), 50)))).all()
    completed = failed = 0
    for row in rows:
        try:
            await invalidate_one(row=row, sessions=sessions, engine=engine, driver=driver, materialize=materialize)
            completed += 1
        except Exception as exc:
            failed += 1
            # Rotate failed work so one damaged/deleted project cannot starve others.
            async with sessions() as db:
                await db.execute(update(Pending).where(Pending.project_id == row.project_id,
                    Pending.chapter_id == row.chapter_id, Pending.revision == row.revision)
                    .values(last_error=type(exc).__name__[:128], updated_at=now()))
                await db.commit()
            logger.warning('Entity invalidation retained for retry: %s', type(exc).__name__)
    return {'examined': len(rows), 'completed': completed, 'failed': failed}
