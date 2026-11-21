"""Atomic, fenced replacement of one chapter's owned graph facts.

Legacy unowned facts are retained; their migration/read gating is separate.
"""
from contextlib import asynccontextmanager
import json
import uuid

from app.services.entity_timeline import EntityTimelineService, CharacterSnapshot
from app.services.chapter_entity_extraction import validate_entity_changes
from app.services.relationship_graph_write import GUARD_CONSTRAINT, lock_relationship_graph
from app.services.relationship_write_contract import relationship_suppression_key
from app.services.relationship_identity import relationship_record_id


ENTITY_PROJECTION_VERSION = 3


class ProjectionSuperseded(RuntimeError):
    pass


class TransactionDriver:
    def __init__(self, tx):
        self.tx = tx

    @asynccontextmanager
    async def session(self):
        yield self.tx


class VersionedTimeline(EntityTimelineService):
    def __init__(self, tx, source):
        super().__init__(TransactionDriver(tx), strict_writes=True)
        self.tx = tx
        self.source = source

    def params(self, **values):
        return dict(pid=self.source.project_id, chapter_id=self.source.chapter_id,
                    source_hash=self.source.source_hash, **values)

    async def get_character_state_at(self, project_id, name, chapter_idx):
        # apply_entity_changes may combine multiple changes inside this chapter,
        # but must never copy a previous chapter's facts under this source hash.
        record = await (await self.tx.run(
            'MATCH (:Character {project_id:$pid,name:$name})-[:HAS_STATE]->(s:CharacterState) '
            'WHERE s.source_chapter_id=$chapter_id AND s.source_hash=$source_hash '
            'AND s.chapter_start=$idx AND s.source_mode="chapter_delta" '
            'RETURN s.status_json AS status', **self.params(name=name, idx=chapter_idx),
        )).single()
        return CharacterSnapshot(name, json.loads(record['status']), chapter_idx) if record else None

    async def update_character_state(self, project_id, name, chapter_idx, state_json):
        await self.tx.run(
            'MATCH (c:Character {project_id:$pid,name:$name}) '
            'MERGE (c)-[:HAS_STATE]->(s:CharacterState {source_chapter_id:$chapter_id,chapter_start:$idx}) '
            'ON CREATE SET s.id=$id '
            'SET s.project_id=$pid,s.source_hash=$source_hash,s.status_json=$state,s.source_mode="chapter_delta"',
            **self.params(name=name, idx=chapter_idx, id=str(uuid.uuid4()), state=json.dumps(state_json, ensure_ascii=False)),
        )

    async def add_relationship(self, project_id, char1, char2, rel_type, chapter_idx, *, evidence=''):
        await self.add_character(project_id, char1)
        await self.add_character(project_id, char2)
        key = relationship_suppression_key(project_id, char1, char2, {
            'source_chapter_id': self.source.chapter_id, 'source_hash': self.source.source_hash, 'type': rel_type})
        await self.tx.run(
            'MATCH (a:Character {project_id:$pid,name:$c1}),(b:Character {project_id:$pid,name:$c2}) '
            'WHERE NOT EXISTS { MATCH (:RelationshipSuppression {key:$suppression}) } '
            'AND NOT EXISTS { MATCH (a)-[manual:RELATES_TO]->(b) '
            'WHERE manual.type=$kind AND manual.chapter_start=$idx AND manual.origin="author" } '
            'MERGE (a)-[r:RELATES_TO {project_id:$pid,source_name:$c1,target_name:$c2,type:$kind,chapter_start:$idx}]->(b) '
            'SET r.source_chapter_id=$chapter_id,r.source_hash=$source_hash,r.event_kind="new",'
            'r.record_id=$record_id,r.raw_type=$kind,r.evidence=$evidence,r.replaces=null',
            **self.params(c1=char1, c2=char2, kind=rel_type, idx=chapter_idx, suppression=key,
                record_id=relationship_record_id(project_id, char1, char2, rel_type,
                                                 self.source.chapter_id, self.source.source_hash), evidence=evidence),
        )

    async def update_relationship(self, project_id, char1, char2, rel_type, chapter_idx, *, evidence='', replaces=None):
        await self.add_relationship(project_id, char1, char2, rel_type, chapter_idx, evidence=evidence)
        await self.tx.run(
            'MATCH (:Character {project_id:$pid,name:$c1})-[r:RELATES_TO]->(:Character {project_id:$pid,name:$c2}) '
            'WHERE r.source_chapter_id=$chapter_id AND r.source_hash=$source_hash AND r.type=$kind '
            'SET r.event_kind="change",r.replaces=$replaces',
            **self.params(c1=char1,c2=char2,kind=rel_type,replaces=replaces),
        )

    async def _set_character_location(self, project_id, character_name, location_name, chapter_idx):
        """同章已经有地点时不再新建 ``AT_LOCATION`` 边。

        ``AT_LOCATION`` 上有唯一约束 ``(project_id, character_name, chapter_start)``。
        实测(2026-11-14 连续 24 章)第 11 章:周芸先在医生办公室、后回病房,
        第二次 MERGE 匹配不到已有边(终点不同),便试图新建同键的边,Neo4j 直接抛
        ``ConstraintValidationFailed``,**整章的实体投影全部丢失**(那一章之后
        角色状态、关系、地点都没有落库)。

        改成:同章已有地点就把第二个地点记进 ``alternate_locations``,不建边。
        信息不丢,约束也不破。
        """
        await self.tx.run(
            'MATCH (c:Character {project_id:$pid,name:$name}) '
            'OPTIONAL MATCH (c)-[existing:AT_LOCATION '
            '{project_id:$pid,character_name:$name,chapter_start:$idx}]->(:Location) '
            'FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END | '
            'MERGE (l:Location {project_id:$pid,name:$location}) ON CREATE SET l.id=$id '
            'MERGE (c)-[r:AT_LOCATION '
            '{project_id:$pid,character_name:$name,chapter_start:$idx}]->(l) '
            'SET r.source_chapter_id=$chapter_id,r.source_hash=$source_hash,'
            'r.location_name=$location) '
            'FOREACH (_ IN CASE WHEN existing IS NULL THEN [] ELSE [1] END | '
            'SET existing.alternate_locations = '
            'coalesce(existing.alternate_locations, []) + [$location])',
            **self.params(location=location_name, name=character_name, idx=chapter_idx,
                          id=str(uuid.uuid4())),
        )


def _capture_manual_end(alias):
    return (f'FOREACH (_ IN CASE WHEN {alias}.source_chapter_id IS NULL AND {alias}.projection_managed IS NULL '
            f'THEN [1] ELSE [] END | SET {alias}.projection_managed=true,{alias}.projection_base_end={alias}.chapter_end) ')


def _set_end(alias):
    return (f'SET {alias}.chapter_end=CASE WHEN {alias}.source_chapter_id IS NULL AND '
            f'{alias}.projection_base_end IS NOT NULL AND (end_idx IS NULL OR {alias}.projection_base_end<end_idx) '
            f'THEN {alias}.projection_base_end ELSE end_idx END')


async def _backfill_relationship_ids(tx, project_id, names):
    """Derive legacy occurrence IDs in bounded writes within the owning transaction.

    elementId is used only to address the row being updated, never as a model
    reference. A rebuilt edge gets the same ID only for the exact same source.
    """
    result = await tx.run(
        'MATCH (c:Character {project_id:$pid})-[r:RELATES_TO]->(n:Character {project_id:$pid}) '
        'WHERE c.name IN $names AND coalesce(r.origin,"")<>"author" '
        'AND r.source_chapter_id IS NOT NULL AND r.source_hash IS NOT NULL AND r.record_id IS NULL '
        'RETURN elementId(r) AS edge_id,c.name AS name,n.name AS target,r.type AS rel_type,'
        'r.raw_type AS raw_type,r.source_chapter_id AS source_chapter_id,r.source_hash AS source_hash',
        pid=project_id, names=names)
    rows = [dict(edge_id=row['edge_id'], record_id=relationship_record_id(
        project_id, row['name'], row['target'], row['rel_type'], row['source_chapter_id'],
        row['source_hash'], row['raw_type'])) async for row in result]
    for start in range(0, len(rows), 200):
        await (await tx.run('UNWIND $rows AS row '
            'MATCH (:Character {project_id:$pid})-[r:RELATES_TO]->(:Character {project_id:$pid}) '
            'WHERE elementId(r)=row.edge_id AND r.record_id IS NULL SET r.record_id=row.record_id',
            pid=project_id, rows=rows[start:start+200])).consume()


async def repair_relationship_intervals(tx, project_id, names):
    """End the exact earlier occurrence, never every relationship of a pair."""
    await _backfill_relationship_ids(tx, project_id, names)
    await tx.run(
        'MATCH (c:Character {project_id:$pid})-[r:RELATES_TO]->(n:Character {project_id:$pid}) '
        'WHERE c.name IN $names AND coalesce(r.origin,"")<>"author" '
        + _capture_manual_end('r') +
        'WITH c,n,collect(r) AS edges UNWIND edges AS r '
        'WITH r,[other IN edges WHERE other.chapter_start>r.chapter_start AND '
        '((other.type=r.type AND '
        '(CASE WHEN trim(coalesce(other.raw_type,""))="" THEN other.type ELSE other.raw_type END)='
        '(CASE WHEN trim(coalesce(r.raw_type,""))="" THEN r.type ELSE r.raw_type END)) OR '
        '(other.event_kind="change" AND r.record_id IN coalesce(other.replaces,[]))) '
        '| other.chapter_start] AS successors '
        'WITH r,CASE WHEN size(successors)=0 THEN null ELSE '
        'reduce(earliest=successors[0],value IN successors | CASE WHEN value<earliest THEN value ELSE earliest END)-1 END AS end_idx '
        + _set_end('r'), pid=project_id, names=names)


async def _repair_intervals(tx, project_id, names):
    """Repair affected derived intervals, preserving manual terminal dates."""
    await tx.run(
        'MATCH (c:Character {project_id:$pid})-[:HAS_STATE]->(s:CharacterState) '
        'WHERE c.name IN $names ' + _capture_manual_end('s') +
        'WITH c,s ORDER BY s.chapter_start,CASE WHEN s.source_chapter_id IS NULL THEN 0 ELSE 1 END,s.id '
        'WITH c,collect(s) AS states UNWIND range(0,size(states)-1) AS i '
        'WITH states[i] AS s,CASE WHEN i+1<size(states) THEN states[i+1].chapter_start-1 ELSE null END AS end_idx '
        + _set_end('s'), pid=project_id, names=names,
    )
    await tx.run(
        'MATCH (c:Character {project_id:$pid})-[r:AT_LOCATION]->(n) WHERE c.name IN $names '
        + _capture_manual_end('r') +
        'WITH c,r ORDER BY r.chapter_start,CASE WHEN r.source_chapter_id IS NULL THEN 0 ELSE 1 END '
        'WITH c,collect(r) AS edges UNWIND range(0,size(edges)-1) AS i '
        'WITH edges[i] AS r,CASE WHEN i+1<size(edges) THEN edges[i+1].chapter_start-1 ELSE null END AS end_idx '
        + _set_end('r'), pid=project_id, names=names,
    )
    await repair_relationship_intervals(tx, project_id, names)


async def project_entity_payload(driver, source, payload, payload_hash, token):
    """One Neo4j transaction replaces owned facts and commits its source receipt."""
    validate_entity_changes(payload, source.content)
    async with driver.session() as session:
        await (await session.run(GUARD_CONSTRAINT)).consume()
        await session.run('CREATE CONSTRAINT IF NOT EXISTS FOR (m:ChapterEntityProjection) '
                          'REQUIRE (m.project_id,m.chapter_id) IS UNIQUE')
    async def apply(tx):
        await lock_relationship_graph(tx, source.project_id)
        record = await (await tx.run(
            'MERGE (m:ChapterEntityProjection {project_id:$pid,chapter_id:$chapter}) '
            'ON CREATE SET m.token=0 SET m.token=m.token '
            'WITH m WHERE m.token <= $token '
            'SET m.token=$token RETURN m.source_hash AS source_hash,m.payload_hash AS payload_hash,m.schema_version AS schema_version',
            pid=source.project_id, chapter=source.chapter_id, token=token,
        )).single()
        if record is None:
            raise ProjectionSuperseded('a newer graph projection owns this chapter')
        if record['source_hash'] == source.source_hash and record['payload_hash'] == payload_hash and record['schema_version'] == ENTITY_PROJECTION_VERSION:
            return False
        names = {item.get(key) for items in payload.values() for item in items
                 for key in ('name', 'character', 'char1', 'char2') if item.get(key)}
        old = await tx.run(
            'MATCH (c:Character {project_id:$pid})-[r]->(n) '
            'WHERE r.source_chapter_id=$chapter OR n.source_chapter_id=$chapter '
            'RETURN DISTINCT c.name AS name', pid=source.project_id, chapter=source.chapter_id,
        )
        names.update([row['name'] async for row in old])
        await tx.run(
            'MATCH (:Character {project_id:$pid})-[:HAS_STATE]->(s:CharacterState {source_chapter_id:$chapter}) '
            'DETACH DELETE s', pid=source.project_id, chapter=source.chapter_id,
        )
        await tx.run(
            'MATCH (:Character {project_id:$pid})-[r]->() WHERE r.source_chapter_id=$chapter DELETE r',
            pid=source.project_id, chapter=source.chapter_id,
        )
        await _repair_intervals(tx, source.project_id, list(names))
        service = VersionedTimeline(tx, source)
        await service.apply_entity_changes(source.project_id, source.global_idx, source.content, payload)
        await _repair_intervals(tx, source.project_id, list(names))
        await tx.run(
            'MATCH (m:ChapterEntityProjection {project_id:$pid,chapter_id:$chapter}) '
            'SET m.source_hash=$hash,m.payload_hash=$payload_hash,m.global_idx=$idx,m.status="applied",m.schema_version=$version',
            pid=source.project_id, chapter=source.chapter_id, hash=source.source_hash,
            payload_hash=payload_hash, idx=source.global_idx, version=ENTITY_PROJECTION_VERSION,
        )
        return True
    async with driver.session() as session:
        return await session.execute_write(apply)


async def invalidate_entity_projection(driver, *, project_id, chapter_id, current_hash, token):
    """Remove only this chapter's owned stale facts; retain a current projection.

    Keep the marker and its fencing token even after deletion, so a delayed
    pre-deletion writer cannot resurrect the removed facts.
    """
    async with driver.session() as session:
        await (await session.run(GUARD_CONSTRAINT)).consume()
        await session.run('CREATE CONSTRAINT IF NOT EXISTS FOR (m:ChapterEntityProjection) '
                          'REQUIRE (m.project_id,m.chapter_id) IS UNIQUE')
    async def invalidate(tx):
        await lock_relationship_graph(tx, project_id)
        record = await (await tx.run(
            'MERGE (m:ChapterEntityProjection {project_id:$pid,chapter_id:$chapter}) '
            'ON CREATE SET m.token=0 SET m.token=m.token '
            'WITH m WHERE m.token <= $token SET m.token=$token '
            'RETURN m.source_hash AS source_hash,m.status AS status',
            pid=project_id, chapter=chapter_id, token=token,
        )).single()
        if record is None:
            raise ProjectionSuperseded('a newer graph projection owns this chapter')
        if current_hash and record['source_hash'] == current_hash and record['status'] == 'applied':
            return False
        old = await tx.run(
            'MATCH (c:Character {project_id:$pid})-[r]->(n) '
            'WHERE r.source_chapter_id=$chapter OR n.source_chapter_id=$chapter '
            'RETURN DISTINCT c.name AS name', pid=project_id, chapter=chapter_id,
        )
        names = [row['name'] async for row in old]
        await tx.run(
            'MATCH (:Character {project_id:$pid})-[:HAS_STATE]->(s:CharacterState {source_chapter_id:$chapter}) '
            'DETACH DELETE s', pid=project_id, chapter=chapter_id,
        )
        await tx.run(
            'MATCH (:Character {project_id:$pid})-[r]->() WHERE r.source_chapter_id=$chapter DELETE r',
            pid=project_id, chapter=chapter_id,
        )
        await _repair_intervals(tx, project_id, names)
        await tx.run(
            'MATCH (m:ChapterEntityProjection {project_id:$pid,chapter_id:$chapter}) '
            'SET m.source_hash=null,m.payload_hash=null,m.global_idx=null,m.status="invalidated"',
            pid=project_id, chapter=chapter_id,
        )
        return True
    async with driver.session() as session:
        return await session.execute_write(invalidate)
