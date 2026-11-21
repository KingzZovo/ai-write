"""Confirm author changes only after PG projection; recover without reapplying edits."""
import asyncio
from uuid import UUID

from sqlalchemy import select

from app.models.project import Project
from app.services.entity_materialization_lock import require_materialization_lock, MaterializationBusy
from app.services.relationship_graph_write import apply_relationship_graph_write, acknowledge_relationship_write
from app.services.relationship_projection import project_relationship_pair, relationship_projection_basis
from app.services.relationship_write_contract import (
    RelationshipWriteConflict, RelationshipWriteUnconfirmed, RelationshipSyncPending, RelationshipProjectUnavailable,
)
from app.services.recovery_deadline import bounded_recovery


async def require_relationship_project(db, project_id, *, allow_deleted=False):
    query = select(Project.id).where(Project.id == UUID(str(project_id)))
    if not allow_deleted:
        query = query.where(Project.deleted_at.is_(None))
    if (await db.execute(query.with_for_update(read=True)
            .execution_options(autoflush=False))).scalar_one_or_none() is None:
        raise RelationshipProjectUnavailable('project_unavailable')


async def apply_author_relationship_write(db, driver, command):
    graph_applied = False
    try:
        async with asyncio.timeout(90):
            await require_materialization_lock(db, command.project_id)
            await require_relationship_project(db, command.project_id)
            projection_id, projection_before = await relationship_projection_basis(db, command.project_id,
                command.source, command.target, command.rel_type)
            receipt = await apply_relationship_graph_write(driver, command,
                projection_id=projection_id, projection_before=projection_before)
            graph_applied = receipt['outcome'] == 'applied'
            await project_relationship_pair(db, driver, command.project_id, command.source, command.target)
            await db.commit()
    except RelationshipWriteConflict as exc:
        await db.rollback()
        if graph_applied:
            raise RelationshipSyncPending(type(exc).__name__) from exc
        raise
    except (RelationshipProjectUnavailable, MaterializationBusy) as exc:
        await db.rollback()
        if graph_applied:
            raise RelationshipSyncPending(type(exc).__name__) from exc
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        error = RelationshipSyncPending if graph_applied else RelationshipWriteUnconfirmed
        raise error(type(exc).__name__) from exc
    if receipt['outcome'] != 'applied':
        raise RelationshipWriteConflict(receipt.get('reason') or 'source_changed')
    try:
        async with asyncio.timeout(10):
            await acknowledge_relationship_write(driver, receipt)
    except Exception:
        pass  # SQL committed; the retained receipt can request another sync.
    return {'status': 'saved', 'request_id': command.id, 'operation': command.operation,
            'source': command.source, 'target': command.target, 'rel_type': command.changes.get('type', command.rel_type)}


@bounded_recovery
async def replay_relationship_writes(driver, *, sessions, limit=20):
    async with driver.session() as session:
        rows = [dict(row) async for row in await session.run(
            'MATCH (w:RelationshipWrite) WHERE w.outcome="applied" AND w.sync_pending=true '
            'AND coalesce(w.sync_after,0)<=timestamp() '
            'RETURN properties(w) AS receipt ORDER BY coalesce(w.sync_after,0),w.id LIMIT $limit',
            limit=min(max(limit, 1), 100))]
    completed = failed = 0
    for item in rows:
        receipt = item['receipt']
        try:
            async with asyncio.timeout(60):
                async with sessions() as db:
                    await require_materialization_lock(db, receipt['project_id'])
                    await require_relationship_project(db, receipt['project_id'], allow_deleted=True)
                    await project_relationship_pair(db, driver, receipt['project_id'], receipt['source_name'], receipt['target_name'])
                    await db.commit()
                await acknowledge_relationship_write(driver, receipt)
            completed += 1
        except Exception:
            failed += 1
            try:
                async with asyncio.timeout(10):
                    async with driver.session() as session:
                        await (await session.run('MATCH (w:RelationshipWrite {id:$rid,project_id:$pid}) '
                            'WHERE w.sync_pending=true SET w.sync_attempts=coalesce(w.sync_attempts,0)+1 '
                            'WITH w SET w.sync_after=timestamp()+toInteger(CASE WHEN w.sync_attempts>=7 '
                            'THEN 3600000 ELSE 60000*(2^(w.sync_attempts-1)) END)',
                            rid=receipt['id'], pid=receipt['project_id'])).consume()
            except Exception:
                pass
    return {'examined': len(rows), 'completed': completed, 'failed': failed}
