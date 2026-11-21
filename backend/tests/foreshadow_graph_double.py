"""Stateful transaction double; does not claim to implement real Neo4j locks."""
import asyncio
from copy import deepcopy


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows
    async def single(self): return deepcopy(self.row)
    async def data(self): return deepcopy(self.rows or [])
    async def consume(self): pass


class WriteGraph:
    def __init__(self):
        self.nodes, self.receipts, self.calls = {}, {}, []
        self.writes = 0
        self.fail_receipt = self.fail_ack = self.lose_commit_ack = False
        self.lock = asyncio.Lock()
        self.now = 1791446400000

    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def execute_write(self, callback):
        async with self.lock:
            previous = deepcopy((self.nodes, self.receipts, self.writes))
            try:
                result = await callback(self)
            except BaseException:
                self.nodes, self.receipts, self.writes = previous
                raise
            if self.lose_commit_ack:
                self.lose_commit_ack = False
                raise ConnectionError('lost graph commit acknowledgement')
            return result

    async def run(self, query, **p):
        self.calls.append((query, p))
        if query.startswith('CREATE CONSTRAINT'): return Result()
        if query.startswith('MERGE (r:ForeshadowWrite'):
            assert 'write_lock=coalesce' in query
            receipt = self.receipts.setdefault(p['rid'], dict(id=p['rid'], project_id=p['pid'],
                foreshadow_id=p['fid'], command_sha256=p['digest'], operation=p['operation']))
            return Result({'receipt': receipt})
        if 'RETURN properties(f) AS node' in query:
            key = (p['pid'], p['fid'])
            if query.startswith('MERGE (f:Foreshadow') and key not in self.nodes:
                self.nodes[key] = dict(project_id=p['pid'], id=p['fid'], creation_token=p['nonce'])
            if 'nonce' in p:
                assert 'confirmation_write_lock=coalesce' in query
            return Result({'node': self.nodes[key]}) if key in self.nodes else Result()
        if 'SET f += $properties' in query:
            node = self.nodes[(p['pid'], p['fid'])]
            node.update(deepcopy(p['properties']))
            for key in list(node):
                if node[key] is None or key == 'creation_token': del node[key]
            self.writes += 1
            return Result()
        if 'DETACH DELETE f' in query:
            del self.nodes[(p['pid'], p['fid'])]
            self.writes += 1
            return Result()
        if 'SET r.outcome=$outcome' in query:
            if self.fail_receipt: raise RuntimeError('receipt transaction failed')
            self.receipts[p['rid']].update(outcome=p['outcome'], reason=p['reason'], sync_pending=p['pending'],
                decided_at=self.now, sync_after=0, sync_attempts=0)
            return Result()
        if 'SET r.sync_pending=false' in query:
            if self.fail_ack: raise ConnectionError('lost sync acknowledgement')
            receipt = self.receipts[p['rid']]
            assert (receipt['project_id'], receipt['command_sha256']) == (p['pid'], p['digest'])
            receipt['sync_pending'] = False
            return Result()
        if 'RETURN r.id AS id' in query:
            rows = sorted((r for r in self.receipts.values() if r.get('outcome') == 'applied'
                and r.get('sync_pending') and r.get('sync_after', 0) <= self.now),
                key=lambda r: (r.get('sync_after', 0), r['id']))[:p['limit']]
            return Result(rows=[{key: row[key] for key in ('id', 'project_id', 'foreshadow_id', 'command_sha256', 'operation')} for row in rows])
        if 'SET r.sync_attempts=' in query:
            receipt = self.receipts[p['rid']]
            receipt['sync_attempts'] += 1
            receipt['sync_after'] = self.now + min(3600000, 60000 * 2 ** (receipt['sync_attempts'] - 1))
            return Result()
        raise AssertionError(query)
