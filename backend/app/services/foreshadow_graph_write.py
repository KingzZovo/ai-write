"""Atomic graph CRUD with a durable decision and pending projection receipt."""
import json
from uuid import uuid4

from app.services.foreshadow_graph_confirmation import projected_foreshadow, GraphConfirmationInvalid
from app.services.foreshadow_provenance import foreshadow_source_hash
from app.services.foreshadow_write_contract import (
    ForeshadowWriteConflict, receipt_for, require_same_receipt,
)


async def apply_graph_write(driver, command):
    expected = receipt_for(command, 'neo4j', 'applied')
    params = dict(rid=str(command.id), pid=str(command.project_id), fid=str(command.foreshadow_id),
                  digest=command.sha256, operation=command.operation)

    async def write(tx):
        row = await (await tx.run(
            'MERGE (r:ForeshadowWrite {id:$rid}) '
            'ON CREATE SET r.project_id=$pid,r.foreshadow_id=$fid,r.command_sha256=$digest,r.operation=$operation '
            'SET r.write_lock=coalesce(r.write_lock,0)+1 RETURN properties(r) AS receipt', **params)).single()
        receipt = row['receipt']
        require_same_receipt(receipt, expected)
        if receipt.get('outcome') in ('applied', 'rejected'):
            return {**expected, 'outcome': receipt['outcome'], 'reason': receipt.get('reason')}
        if receipt.get('outcome') is not None:
            raise ForeshadowWriteConflict('invalid_write_receipt')

        nonce = str(uuid4())
        if command.operation == 'create':
            query = ('MERGE (f:Foreshadow {project_id:$pid,id:$fid}) '
                'ON CREATE SET f.creation_token=$nonce ')
        else:
            query = 'MATCH (f:Foreshadow {project_id:$pid,id:$fid}) '
        row = await (await tx.run(query +
            'SET f.confirmation_write_lock=coalesce(f.confirmation_write_lock,0)+1 '
            'RETURN properties(f) AS node', **params, nonce=nonce)).single()
        reason = None
        if command.operation == 'create':
            if row is None or row['node'].get('creation_token') != nonce:
                reason = 'already_exists'
        elif row is None:
            reason = 'source_missing'
        else:
            try:
                current = projected_foreshadow(row['node'])
                if foreshadow_source_hash(current) != command.expected_version:
                    reason = 'source_changed'
                elif command.operation == 'resolve' and current['status'] == 'resolved':
                    reason = 'already_resolved'
            except GraphConfirmationInvalid:
                reason = 'invalid_source'

        if reason is None:
            if command.operation == 'delete':
                result = await tx.run('MATCH (f:Foreshadow {project_id:$pid,id:$fid}) DETACH DELETE f', **params)
            else:
                properties = dict(command.changes)
                for key in ('resolve_conditions_json', 'resolution_blueprint_json'):
                    if key in properties:
                        properties[key] = json.dumps(properties[key], ensure_ascii=False, allow_nan=False)
                result = await tx.run('MATCH (f:Foreshadow {project_id:$pid,id:$fid}) '
                    'SET f += $properties REMOVE f.creation_token', **params, properties=properties)
            await result.consume()

        outcome = 'applied' if reason is None else 'rejected'
        result = await tx.run('MATCH (r:ForeshadowWrite {id:$rid}) '
            'SET r.outcome=$outcome,r.reason=$reason,r.decided_at=timestamp(),'
            'r.sync_pending=$pending,r.sync_after=0,r.sync_attempts=0',
            **params, outcome=outcome, reason=reason, pending=outcome == 'applied')
        await result.consume()
        return {**expected, 'outcome': outcome, 'reason': reason}

    async with driver.session() as session:
        for query in (
            'CREATE CONSTRAINT foreshadow_write_id IF NOT EXISTS FOR (r:ForeshadowWrite) REQUIRE r.id IS UNIQUE',
            'CREATE CONSTRAINT IF NOT EXISTS FOR (f:Foreshadow) REQUIRE (f.project_id,f.id) IS UNIQUE',
        ):
            await (await session.run(query)).consume()
        return await session.execute_write(write)


async def acknowledge_graph_write(driver, receipt):
    async with driver.session() as session:
        result = await session.run('MATCH (r:ForeshadowWrite {id:$rid,project_id:$pid,command_sha256:$digest}) '
            'WHERE r.outcome="applied" SET r.sync_pending=false',
            rid=str(receipt['id']), pid=str(receipt['project_id']), digest=receipt['command_sha256'])
        await result.consume()
