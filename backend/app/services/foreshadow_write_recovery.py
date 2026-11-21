"""Retry graph -> PG synchronization without replaying author mutations."""
import asyncio
from uuid import UUID

from app.db.session import async_session_factory
from app.services.entity_materialization_lock import require_materialization_lock
from app.services.foreshadow_graph_write import acknowledge_graph_write
from app.services.foreshadow_projection import project_current_foreshadow
from app.services.foreshadow_writes import record_write_receipt, require_project
from app.services.recovery_deadline import bounded_recovery


@bounded_recovery
async def replay_foreshadow_writes(driver, *, sessions=async_session_factory, limit=20):
    async with driver.session() as session:
        rows = await (await session.run(
            'MATCH (r:ForeshadowWrite) WHERE r.outcome="applied" AND r.sync_pending=true '
            'AND coalesce(r.sync_after,0) <= timestamp() '
            'RETURN r.id AS id,r.project_id AS project_id,r.foreshadow_id AS foreshadow_id,'
            'r.command_sha256 AS command_sha256,r.operation AS operation '
            'ORDER BY coalesce(r.sync_after,0),r.id LIMIT $limit', limit=min(max(limit, 1), 100))).data()
    completed = failed = 0
    for row in rows:
        try:
            receipt = {**row, **{key: UUID(row[key]) for key in ('id', 'project_id', 'foreshadow_id')},
                       'source': 'neo4j', 'outcome': 'applied', 'reason': None}
            async with asyncio.timeout(60):
                async with sessions() as db:
                    await require_materialization_lock(db, receipt['project_id'])
                    await require_project(db, receipt['project_id'], allow_deleted=True)
                    await project_current_foreshadow(db, driver, receipt['project_id'], receipt['foreshadow_id'])
                    await record_write_receipt(db, receipt)
                    await db.commit()
                await acknowledge_graph_write(driver, receipt)
            completed += 1
        except Exception:
            failed += 1
            try:
                async with asyncio.timeout(10):
                    async with driver.session() as session:
                        result = await session.run('MATCH (r:ForeshadowWrite {id:$rid,project_id:$pid}) '
                            'WHERE r.sync_pending=true SET r.sync_attempts=coalesce(r.sync_attempts,0)+1 '
                            'WITH r SET r.sync_after=timestamp()+toInteger(CASE WHEN r.sync_attempts >= 7 '
                            'THEN 3600000 ELSE 60000*(2^(r.sync_attempts-1)) END)',
                            rid=row['id'], pid=row['project_id'])
                        await result.consume()
            except Exception:
                pass  # The independently retained graph receipt still owns the work.
    return {'examined': len(rows), 'completed': completed, 'failed': failed}
