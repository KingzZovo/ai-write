"""Replay pending author profile projections without changing graph profiles."""
import asyncio
import time
from collections import defaultdict

from app.services.recovery_deadline import bounded_recovery


def now_ms():
    return int(time.time() * 1000)


async def acknowledge_profiles(driver, project_id, receipts):
    async with driver.session() as session:
        result = await session.run(
            'UNWIND $rows AS row MATCH (c:Character {project_id:$pid,name:row.name}) '
            'SET c.profile_write_lock=coalesce(c.profile_write_lock,0)+1 WITH c,row '
            'WHERE c.profile_sync_token=row.token '
            'REMOVE c.profile_sync_token,c.profile_sync_after,c.profile_sync_attempts '
            'RETURN count(c) AS acknowledged', pid=str(project_id), rows=receipts)
        row = await result.single()
        return int(row['acknowledged']) if row else 0


async def defer_profiles(driver, project_id, receipts):
    async with driver.session() as session:
        result = await session.run(
            'UNWIND $rows AS row MATCH (c:Character {project_id:$pid,name:row.name}) '
            'SET c.profile_write_lock=coalesce(c.profile_write_lock,0)+1 WITH c,row '
            'WHERE c.profile_sync_token=row.token '
            'SET c.profile_sync_attempts=coalesce(c.profile_sync_attempts,0)+1 '
            'WITH c SET c.profile_sync_after=$now + '
            'toInteger(CASE WHEN c.profile_sync_attempts >= 7 THEN 3600000 '
            'ELSE 60000 * (2 ^ (c.profile_sync_attempts - 1)) END)',
            pid=str(project_id), rows=receipts, now=now_ms())
        await result.consume()


@bounded_recovery
async def replay_author_profiles(driver, *, materialize=None, limit=20):
    if materialize is None:
        from app.tasks.entity_tasks import _materialize_entities_to_postgres
        materialize = _materialize_entities_to_postgres
    async with driver.session() as session:
        result = await session.run(
            'MATCH (c:Character) WHERE c.profile_sync_token IS NOT NULL '
            'AND coalesce(c.profile_sync_after,0) <= $now '
            'RETURN c.project_id AS project_id,c.name AS name,c.profile_sync_token AS token '
            'ORDER BY coalesce(c.profile_sync_after,0),c.project_id,c.name LIMIT $limit',
            now=now_ms(), limit=min(max(limit, 1), 100))
        rows = await result.data()
    groups = defaultdict(list)
    for row in rows:
        groups[row['project_id']].append({'name': row['name'], 'token': row['token']})
    acknowledged = failed = 0
    for project_id, receipts in groups.items():
        try:
            async with asyncio.timeout(120):
                await materialize(project_id=project_id, chapter_idx=0,
                    caller='characters.profile_recovery', strict=True, graph_driver=driver)
                acknowledged += await acknowledge_profiles(driver, project_id, receipts)
        except Exception:
            failed += 1
            # Failed acknowledgement also keeps work durable. Never clear a
            # newer author's token selected after this batch was read.
            try:
                async with asyncio.timeout(15):
                    await defer_profiles(driver, project_id, receipts)
            except Exception:
                pass  # The graph still owns the pending token for a later pass.
    return {'examined': len(rows), 'projects': len(groups), 'acknowledged': acknowledged, 'failed': failed}
