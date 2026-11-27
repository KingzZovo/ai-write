"""Atomic graph deletion and idempotent PG cleanup staged by every projector."""
import asyncio
import json
from uuid import UUID

from sqlalchemy import delete, select

from app.models.entity_extraction import AppliedCharacterDeletion
from app.models.project import Character, CharacterAppearance, CharacterCognition, CharacterMemoryCard
from app.services.character_profile_write import CharacterProfileConflict, same_profile
from app.services.recovery_deadline import bounded_recovery
from app.services.relationship_graph_write import GUARD_CONSTRAINT, lock_relationship_graph


async def delete_character_graph(driver, project_id, name, expected, token):
    pid, token = str(UUID(str(project_id))), str(UUID(str(token)))
    async with driver.session() as session:
        await (await session.run(GUARD_CONSTRAINT)).consume()
        result = await session.run('CREATE CONSTRAINT IF NOT EXISTS '
            'FOR (c:Character) REQUIRE (c.project_id,c.name) IS UNIQUE')
        await result.consume()
        result = await session.run('CREATE CONSTRAINT character_deletion_token IF NOT EXISTS '
            'FOR (d:CharacterDeletion) REQUIRE (d.project_id,d.token) IS UNIQUE')
        await result.consume()
        async def write(tx):
            await lock_relationship_graph(tx, pid)
            # Receipt lock serializes duplicate requests, including a retry after
            # acknowledgement. Completed receipts remain as idempotency keys.
            row = await (await tx.run(
                'MERGE (d:CharacterDeletion {project_id:$pid,token:$token}) '
                'ON CREATE SET d.name=$name,d.status="preparing" '
                'SET d.write_lock=coalesce(d.write_lock,0)+1 '
                'RETURN d.name AS name,d.status AS status', pid=pid, token=token, name=name)).single()
            if row['name'] != name:
                raise CharacterProfileConflict('删除请求编号已用于其他人物。')
            if row['status'] in ('pending', 'completed'):
                return
            current = await (await tx.run(
                'MATCH (c:Character {project_id:$pid,name:$name}) '
                'SET c.profile_write_lock=coalesce(c.profile_write_lock,0)+1 '
                'RETURN c.profile_json AS profile', pid=pid, name=name)).single()
            if current is None:
                raise CharacterProfileConflict('人物不存在或已删除，请刷新核对。')
            try:
                profile = json.loads(current['profile']) if current['profile'] is not None else {}
            except (ValueError, TypeError):
                raise CharacterProfileConflict('人物档案格式异常，未执行删除。') from None
            if not isinstance(profile, dict) or not same_profile(profile, expected):
                raise CharacterProfileConflict('人物档案已变化，未删除新版本，请刷新核对。')
            result = await tx.run('MATCH (c:Character {project_id:$pid,name:$name})-[:HAS_STATE]->(s) '
                'WHERE NOT EXISTS { MATCH (other:Character)-[:HAS_STATE]->(s) WHERE other <> c } '
                'WITH DISTINCT s DETACH DELETE s', pid=pid, name=name)
            await result.consume()
            result = await tx.run('MATCH (c:Character {project_id:$pid,name:$name}) DETACH DELETE c', pid=pid, name=name)
            await result.consume()
            result = await tx.run('MATCH (d:CharacterDeletion {project_id:$pid,token:$token}) '
                'SET d.status="pending",d.retry_after=0', pid=pid, token=token)
            await result.consume()
        await session.execute_write(write)


async def stage_character_deletions(db, driver, project_id):
    """Must run under materialization lock, before inserting current graph rows."""
    async with driver.session() as session:
        result = await session.run('MATCH (d:CharacterDeletion {project_id:$pid,status:"pending"}) '
            'RETURN d.token AS token,d.name AS name ORDER BY d.token', pid=str(project_id))
        rows = await result.data()
    if not rows: return []
    tokens = [UUID(row['token']) for row in rows]
    applied = set((await db.scalars(select(AppliedCharacterDeletion.token).where(
        AppliedCharacterDeletion.project_id == UUID(str(project_id)),
        AppliedCharacterDeletion.token.in_(tokens)))).all())
    for row in rows:
        token = UUID(row['token'])
        if token in applied: continue
        name = row['name']
        await db.execute(delete(Character).where(Character.project_id == project_id, Character.name == name))
        for model in (CharacterAppearance, CharacterCognition, CharacterMemoryCard):
            await db.execute(delete(model).where(model.project_id == project_id, model.character_name == name))
        db.add(AppliedCharacterDeletion(token=token, project_id=UUID(str(project_id))))
        applied.add(token)
    return [str(token) for token in tokens]


async def acknowledge_character_deletions(driver, project_id, tokens):
    if not tokens: return
    async with driver.session() as session:
        result = await session.run('MATCH (d:CharacterDeletion {project_id:$pid}) WHERE d.token IN $tokens '
            'SET d.status="completed"', pid=str(project_id), tokens=tokens)
        await result.consume()


@bounded_recovery
async def replay_character_deletions(driver, *, materialize=None, limit=20):
    if materialize is None:
        from app.tasks.entity_tasks import _materialize_entities_to_postgres
        materialize = _materialize_entities_to_postgres
    async with driver.session() as session:
        # RETURN 里带聚合(collect)时,ORDER BY 只能引用 RETURN 投影里的东西:
        # 裸写 min(...) 会被 Neo4j 判成 CypherSyntaxError
        # ("In a WITH/RETURN with DISTINCT or an aggregation, it is not possible to
        #  access variables declared before")。这个恢复任务因此每 60 秒抛一次错,
        # 角色删除的重放一直没跑成 —— 把最早的到期时间也投影出来,再按它排序。
        rows = await (await session.run('MATCH (d:CharacterDeletion {status:"pending"}) '
            'WHERE coalesce(d.retry_after,0) <= timestamp() '
            'RETURN d.project_id AS project_id,collect(d.token) AS tokens,'
            'min(coalesce(d.retry_after,0)) AS due '
            'ORDER BY due,project_id LIMIT $limit', limit=min(max(limit, 1), 100))).data()
    completed = failed = 0
    for row in rows:
        try:
            async with asyncio.timeout(120):
                await materialize(project_id=row['project_id'], chapter_idx=0,
                    caller='characters.deletion_recovery', strict=True, graph_driver=driver)
            completed += 1
        except Exception:
            failed += 1
            try:
                async with asyncio.timeout(15):
                    async with driver.session() as session:
                        result = await session.run('MATCH (d:CharacterDeletion {project_id:$pid,status:"pending"}) '
                            'WHERE d.token IN $tokens SET d.retry_after=timestamp()+60000',
                            pid=row['project_id'], tokens=row['tokens'])
                        await result.consume()
            except Exception:
                pass  # Retained receipt remains recoverable if the graph is down.
    return {'projects': len(rows), 'completed': completed, 'failed': failed}
