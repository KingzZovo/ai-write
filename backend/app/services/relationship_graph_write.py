"""Author relationship decisions, history and projection receipts in one graph transaction."""
from uuid import uuid4

from app.services.entity_materialization_lock import MaterializationBusy
from app.services.relationship_write_contract import (
    RelationshipWriteConflict, frozen_json, relationship_snapshot, relationship_suppression_key,
)


GUARD_CONSTRAINT = ('CREATE CONSTRAINT relationship_project_guard IF NOT EXISTS '
                    'FOR (g:RelationshipProjectGuard) REQUIRE g.project_id IS UNIQUE')


async def lock_relationship_graph(tx, project_id):
    await (await tx.run('MERGE (g:RelationshipProjectGuard {project_id:$pid}) '
        'SET g.write_lock=coalesce(g.write_lock,0)+1', pid=str(project_id))).consume()


async def read_pair(tx, project_id, source, target):
    result = await tx.run('MATCH (a:Character {project_id:$pid,name:$src})-[r:RELATES_TO]->'
        '(b:Character {project_id:$pid,name:$tgt}) '
        'RETURN elementId(r) AS edge_id,properties(r) AS properties',
        pid=str(project_id), src=source, tgt=target)
    return [dict(row) async for row in result]


async def require_relationship_pair_ready(tx, project_id, source, target):
    """A pending character deletion must retire old PG identities first."""
    pending = await (await tx.run(
        'MATCH (d:CharacterDeletion {project_id:$pid,status:"pending"}) '
        'WHERE d.name IN $names RETURN d.name AS pending_name LIMIT 1',
        pid=str(project_id), names=[source, target])).single()
    if pending:
        raise MaterializationBusy('relationship_character_cleanup_pending')


async def read_relationship_snapshot(driver, project_id, source, target, rel_type):
    async with driver.session() as session:
        return relationship_snapshot(project_id, source, target, rel_type,
                                     await read_pair(session, project_id, source, target))


async def apply_relationship_graph_write(driver, command, *, projection_id=None, projection_before=None):
    params = dict(rid=command.id, pid=command.project_id, src=command.source, tgt=command.target,
                  rtype=command.rel_type, digest=command.sha256, operation=command.operation)

    async def write(tx):
        row = await (await tx.run('MERGE (w:RelationshipWrite {id:$rid}) '
            'ON CREATE SET w.project_id=$pid,w.command_sha256=$digest,w.operation=$operation,'
            'w.source_name=$src,w.target_name=$tgt,w.rel_type=$rtype '
            'SET w.write_lock=coalesce(w.write_lock,0)+1 RETURN properties(w) AS receipt', **params)).single()
        stored = row['receipt']
        if any(stored.get(key) != value for key, value in (
                ('project_id', command.project_id), ('command_sha256', command.sha256))):
            raise RelationshipWriteConflict('request_id_reused')
        if stored.get('outcome') in ('applied', 'rejected'):
            return stored
        await lock_relationship_graph(tx, command.project_id)
        await require_relationship_pair_ready(tx, command.project_id, command.source, command.target)
        pairs = await read_pair(tx, command.project_id, command.source, command.target)
        snapshot = relationship_snapshot(command.project_id, command.source, command.target, command.rel_type, pairs)
        selected = snapshot['entries']
        projection_ids = {entry['properties']['projection_id'] for entry in selected
                          if entry['properties'].get('projection_id')}
        if len(projection_ids) > 1:
            raise RelationshipWriteConflict('ambiguous_relationship_identity')
        stable_id = next(iter(projection_ids), projection_id or str(uuid4()))
        reason = None
        if command.operation == 'create':
            if selected:
                reason = 'already_exists'
        elif not selected:
            reason = 'source_missing'
        elif snapshot['version'] != command.expected_version:
            reason = 'source_changed'
        if reason is None and command.operation == 'update' and 'type' in command.changes:
            if (command.changes['type'] != command.rel_type and
                    relationship_snapshot(command.project_id, command.source, command.target,
                                          command.changes['type'], pairs)['entries']):
                reason = 'target_exists'
            selected_ids = {row['edge_id'] for row in selected}
            keys = set()
            for row in pairs:
                props = row['properties']
                kind = command.changes['type'] if str(row['edge_id']) in selected_ids else props['type']
                key = (kind, props.get('chapter_start'))
                if key in keys:
                    reason = 'target_exists'
                keys.add(key)
        if reason is None:
            if command.operation == 'create':
                result = await tx.run('MERGE (a:Character {project_id:$pid,name:$src}) '
                    'ON CREATE SET a.id=$aid '
                    'MERGE (b:Character {project_id:$pid,name:$tgt}) ON CREATE SET b.id=$bid '
                    'CREATE (a)-[r:RELATES_TO]->(b) SET r=$properties '
                    'SET r.author_updated_at=timestamp()', **params, aid=str(uuid4()), bid=str(uuid4()),
                    properties={**command.changes, 'project_id': command.project_id,
                        'source_name': command.source, 'target_name': command.target, 'origin': 'author',
                        'author_id': str(uuid4()), 'author_edit_id': command.id, 'author_basis_json': 'null',
                        'projection_id': stable_id})
                await result.consume()
            for entry in selected:
                props = entry['properties']
                suppression = relationship_suppression_key(command.project_id, command.source, command.target, props)
                if suppression:
                    await (await tx.run('MERGE (s:RelationshipSuppression {key:$key}) '
                        'ON CREATE SET s.project_id=$pid,s.source_chapter_id=$chapter,s.source_hash=$hash,'
                        's.source_outline_id=$outline,s.source_outline_digest=$outline_digest,'
                        's.source_name=$src,s.target_name=$tgt,s.original_type=$kind,s.write_id=$rid,'
                        's.created_at=timestamp()', **params, key=suppression, chapter=props.get('source_chapter_id'),
                        hash=props.get('source_hash'), outline=props.get('source_outline_id'),
                        outline_digest=props.get('source_outline_digest'), kind=props['type'])).consume()
                if command.operation == 'delete':
                    result = await tx.run('MATCH (:Character {project_id:$pid})-[r:RELATES_TO]->() '
                        'WHERE elementId(r)=$edge DELETE r', **params, edge=entry['edge_id'])
                else:
                    patch = {**command.changes, 'origin': 'author', 'author_id': props.get('author_id') or str(uuid4()),
                        'author_edit_id': command.id,
                        'projection_id': stable_id,
                        'author_basis_json': props.get('author_basis_json') or frozen_json(props)}
                    result = await tx.run('MATCH (:Character {project_id:$pid})-[r:RELATES_TO]->() '
                        'WHERE elementId(r)=$edge SET r += $patch,r.author_updated_at=timestamp() '
                        'REMOVE r.source_chapter_id,r.source_hash,r.event_kind,'
                        'r.source_outline_id,r.source_outline_digest,r.record_id,r.replaces,r.evidence',
                        **params, edge=entry['edge_id'], patch=patch)
                await result.consume()
            # Removing/changing a closing event must reopen only the automated
            # intervals it derived. Author edges keep their own terminal dates.
            from app.services.entity_graph_projection import repair_relationship_intervals
            await repair_relationship_intervals(tx, command.project_id, [command.source])
        outcome = 'applied' if reason is None else 'rejected'
        result = await tx.run('MATCH (w:RelationshipWrite {id:$rid}) '
            'SET w.outcome=$outcome,w.reason=$reason,w.decided_at=timestamp(),w.sync_pending=$pending,'
            'w.sync_after=0,w.sync_attempts=0,w.before_json=$before,w.changes_json=$changes,'
            'w.projection_before_json=$projection_before '
            'RETURN properties(w) AS receipt', **params, outcome=outcome, reason=reason,
            pending=reason is None, before=frozen_json(selected), changes=frozen_json(command.changes),
            projection_before=frozen_json(projection_before))
        return (await result.single())['receipt']

    async with driver.session() as session:
        for query in (GUARD_CONSTRAINT,
                'CREATE CONSTRAINT relationship_write_id IF NOT EXISTS FOR (w:RelationshipWrite) REQUIRE w.id IS UNIQUE',
                'CREATE CONSTRAINT relationship_suppression_key IF NOT EXISTS FOR (s:RelationshipSuppression) REQUIRE s.key IS UNIQUE'):
            await (await session.run(query)).consume()
        return await session.execute_write(write)


async def acknowledge_relationship_write(driver, receipt):
    async with driver.session() as session:
        await (await session.run('MATCH (w:RelationshipWrite {id:$rid,project_id:$pid,command_sha256:$digest}) '
            'WHERE w.outcome="applied" SET w.sync_pending=false',
            rid=receipt['id'], pid=receipt['project_id'], digest=receipt['command_sha256'])).consume()
