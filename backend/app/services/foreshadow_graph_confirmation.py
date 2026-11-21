"""Atomic author payoff decisions with durable, replay-safe graph receipts."""
import json
from collections.abc import Mapping
from uuid import UUID

from app.services.foreshadow_provenance import foreshadow_source_hash, REVIEWABLE_STATUSES


class GraphConfirmationInvalid(ValueError):
    pass


def projected_foreshadow(node):
    """Use projection conventions, but reject malformed graph data instead of guessing."""
    if not isinstance(node, Mapping):
        raise GraphConfirmationInvalid('missing_foreshadow')
    values = dict(node)
    for key in ('type', 'description'):
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise GraphConfirmationInvalid('invalid_foreshadow_text')
        values[key] = values[key].strip()
    if type(values.get('planted_chapter')) is not int or values['planted_chapter'] < 0:
        raise GraphConfirmationInvalid('invalid_foreshadow_chronology')
    for key, default in (('resolve_conditions_json', []), ('resolution_blueprint_json', {})):
        value = values.get(key)
        if value is None or value == '':
            value = default
        elif isinstance(value, str):
            try: value = json.loads(value)
            except (TypeError, ValueError): raise GraphConfirmationInvalid('invalid_foreshadow_json') from None
        if not isinstance(value, (dict, list)) or (key == 'resolution_blueprint_json' and not isinstance(value, dict)):
            raise GraphConfirmationInvalid('invalid_foreshadow_json')
        values[key] = value
    status = values.get('status')
    if status is None or (isinstance(status, str) and not status.strip()): status = 'planted'
    if not isinstance(status, str) or status.strip() not in {'planned', 'pending', 'planted', 'ripening', 'ready', 'resolved'}:
        raise GraphConfirmationInvalid('invalid_foreshadow_status')
    resolved = values.get('resolved_chapter')
    if resolved is not None and (type(resolved) is not int or resolved < 0):
        raise GraphConfirmationInvalid('invalid_foreshadow_chronology')
    values.update(status=status.strip(), resolved_chapter=resolved, source='neo4j')
    return {key: values[key] for key in ('type', 'description', 'planted_chapter',
        'resolve_conditions_json', 'resolution_blueprint_json', 'status', 'resolved_chapter', 'source')}


async def decide_graph_confirmation(driver, *, review_id, project_id, foreshadow_id,
                                    snapshot_sha256, expected, global_idx, allow_apply):
    """Caller must hold the current chapter lock when allow_apply is true.

    A rejected receipt fences late attempts too. Receipts are independent of
    the story node so deletion after a successful write cannot erase proof.
    """
    params = dict(rid=str(UUID(str(review_id))), pid=str(UUID(str(project_id))),
                  fid=str(UUID(str(foreshadow_id))), digest=snapshot_sha256)
    if (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
            or any(c not in '0123456789abcdef' for c in snapshot_sha256)
            or type(global_idx) is not int or global_idx <= 0
            or expected.get('source') != 'neo4j' or expected.get('status') not in REVIEWABLE_STATUSES):
        raise GraphConfirmationInvalid('invalid_confirmation_command')
    expected_hash = foreshadow_source_hash(expected)

    async def write(tx):
        row = await (await tx.run(
            'MERGE (r:ForeshadowConfirmation {id:$rid}) '
            'ON CREATE SET r.project_id=$pid,r.foreshadow_id=$fid,r.snapshot_sha256=$digest '
            'SET r.write_lock=coalesce(r.write_lock,0)+1 '
            'RETURN properties(r) AS receipt', **params)).single()
        receipt = row['receipt'] if row else None
        if (not isinstance(receipt, Mapping) or receipt.get('id') != params['rid'] or receipt.get('project_id') != params['pid']
                or receipt.get('foreshadow_id') != params['fid'] or receipt.get('snapshot_sha256') != snapshot_sha256):
            raise GraphConfirmationInvalid('confirmation_receipt_mismatch')
        if receipt.get('outcome') in {'applied', 'rejected'}:
            if type(receipt.get('decided_at')) is not int or receipt['decided_at'] <= 0:
                raise GraphConfirmationInvalid('invalid_confirmation_receipt')
            return {key: receipt[key] for key in ('id', 'project_id', 'foreshadow_id', 'snapshot_sha256', 'outcome', 'decided_at')}
        if receipt.get('outcome') is not None:
            raise GraphConfirmationInvalid('invalid_confirmation_receipt')
        apply = False
        if allow_apply:
            row = await (await tx.run(
                'MATCH (f:Foreshadow {project_id:$pid,id:$fid}) '
                'SET f.confirmation_write_lock=coalesce(f.confirmation_write_lock,0)+1 '
                'RETURN properties(f) AS node', **params)).single()
            try:
                current = projected_foreshadow(row['node'] if row else None)
                apply = foreshadow_source_hash(current) == expected_hash
            except GraphConfirmationInvalid:
                apply = False
        if apply:
            result = await tx.run(
                'MATCH (f:Foreshadow {project_id:$pid,id:$fid}) '
                'SET f.status="resolved",f.resolved_chapter=$chapter,f.narrative_proximity=1.0',
                **params, chapter=global_idx)
            await result.consume()
        row = await (await tx.run(
            'MATCH (r:ForeshadowConfirmation {id:$rid}) '
            'SET r.outcome=$outcome,r.decided_at=timestamp() RETURN properties(r) AS receipt',
            **params, outcome='applied' if apply else 'rejected')).single()
        receipt = row['receipt']
        return {key: receipt[key] for key in ('id', 'project_id', 'foreshadow_id', 'snapshot_sha256', 'outcome', 'decided_at')}

    async with driver.session() as session:
        result = await session.run('CREATE CONSTRAINT foreshadow_confirmation_id IF NOT EXISTS '
                                   'FOR (r:ForeshadowConfirmation) REQUIRE r.id IS UNIQUE')
        await result.consume()
        result = await session.run('CREATE CONSTRAINT IF NOT EXISTS '
                                   'FOR (f:Foreshadow) REQUIRE (f.project_id,f.id) IS UNIQUE')
        await result.consume()
        return await session.execute_write(write)
