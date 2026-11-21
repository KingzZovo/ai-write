"""Transactional graph double for author workflow tests, not a Cypher engine."""
import asyncio
from copy import deepcopy
from uuid import uuid4


class Result:
    def __init__(self, rows=()): self.rows = deepcopy(list(rows))
    async def single(self): return self.rows[0] if self.rows else None
    async def consume(self): pass
    async def data(self): return deepcopy(self.rows)
    def __aiter__(self):
        async def rows():
            for row in deepcopy(self.rows): yield row
        return rows()


class RelationshipGraph:
    def __init__(self, project_id, rows=()):
        self.project_id = str(project_id)
        self.edges = deepcopy(list(rows))
        self.receipts, self.suppressions, self.markers = {}, {}, {}
        self.pending_characters = set()
        self.calls = []
        self.mutations = 0
        self.clock = 1000
        self.lose_ack = self.fail_receipt = self.fail_sync_ack = False
        self.lock = asyncio.Lock()
    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def execute_read(self, callback): return await callback(self)
    async def execute_write(self, callback):
        async with self.lock:
            before = deepcopy((self.edges, self.receipts, self.suppressions, self.markers, self.mutations))
            try: result = await callback(self)
            except BaseException:
                self.edges, self.receipts, self.suppressions, self.markers, self.mutations = before
                raise
            if self.lose_ack:
                self.lose_ack = False
                raise ConnectionError('private graph commit acknowledgement')
            return result

    async def run(self, query, **p):
        self.calls.append((query, deepcopy(p)))
        self.clock += 1
        if query.startswith('CREATE CONSTRAINT') or 'MERGE (g:RelationshipProjectGuard' in query:
            return Result()
        if 'RETURN d.name AS pending_name' in query:
            return Result([dict(pending_name=name) for name in p['names'] if name in self.pending_characters])
        if 'MERGE (w:RelationshipWrite' in query:
            row = self.receipts.setdefault(p['rid'], dict(id=p['rid'], project_id=p['pid'], command_sha256=p['digest'],
                operation=p['operation'], source_name=p['src'], target_name=p['tgt'], rel_type=p['rtype']))
            return Result([{'receipt': row}])
        if 'MATCH (w:RelationshipWrite)' in query:
            rows = [dict(receipt=r) for r in self.receipts.values()
                    if r.get('outcome') == 'applied' and r.get('sync_pending') and r.get('sync_after', 0) <= self.clock]
            return Result(rows[:p['limit']])
        if 'MATCH (w:RelationshipWrite {' in query:
            row = self.receipts.get(p['rid'])
            if not row or ('pid' in p and row['project_id'] != p['pid']): return Result()
            if 'w.outcome=$outcome' in query:
                if self.fail_receipt: raise RuntimeError('receipt failed')
                row.update(outcome=p['outcome'], reason=p['reason'], decided_at=self.clock,
                    sync_pending=p['pending'], sync_after=0, sync_attempts=0,
                    before_json=p['before'], changes_json=p['changes'],
                    projection_before_json=p['projection_before'])
            elif 'w.sync_pending=false' in query:
                if self.fail_sync_ack:
                    self.fail_sync_ack = False
                    raise ConnectionError('sync acknowledgement failed')
                if row['command_sha256'] == p['digest']: row['sync_pending'] = False
            else:
                row['sync_attempts'] = row.get('sync_attempts', 0) + 1
                row['sync_after'] = self.clock + 60000
            return Result([{'receipt': row}])
        if 'MERGE (s:RelationshipSuppression' in query:
            self.suppressions.setdefault(p['key'], deepcopy(p))
            return Result()
        if 'RETURN s.key AS owned' in query:
            return Result([{'owned': row['key']} for row in self.suppressions.values()
                if row['pid'] == p['pid'] and row['src'] == p['c1'] and row['tgt'] == p['c2']][:1])
        if 'RETURN elementId(r) AS owned' in query:
            return Result([{'owned': row['edge_id']} for row in self.edges
                if row['project_id'] == p['pid'] and row['source'] == p['c1'] and row['target'] == p['c2']
                and (row['properties'].get('origin') == 'author' or row['properties'].get('source_chapter_id')
                     or row['properties'].get('source_outline_id'))][:1])
        if 'RETURN elementId(r) AS edge_id,properties(r) AS properties' in query:
            return Result([row for row in self.edges if row['project_id'] == str(p['pid'])
                and row['source'] == p['src'] and row['target'] == p['tgt']])
        if 'CREATE (a)-[r:RELATES_TO]->(b) SET r=$properties' in query:
            self.edges.append(dict(edge_id=str(uuid4()), project_id=p['pid'], source=p['src'], target=p['tgt'],
                properties={**deepcopy(p['properties']), 'author_updated_at': self.clock}))
            self.mutations += 1
            return Result()
        if 'WHERE elementId(r)=$edge' in query:
            rows = [r for r in self.edges if r['edge_id'] == p['edge'] and r['project_id'] == p['pid']]
            for row in rows:
                if 'DELETE r' in query: self.edges.remove(row)
                else:
                    row['properties'].update(deepcopy(p['patch']), author_updated_at=self.clock)
                    if 'REMOVE r.source_chapter_id,r.source_hash,r.event_kind' in query:
                        for key in ('source_chapter_id', 'source_hash', 'event_kind', 'source_outline_id', 'source_outline_digest',
                                    'record_id', 'replaces', 'evidence'):
                            row['properties'].pop(key, None)
                self.mutations += 1
            return Result()
        if 'RETURN elementId(r) AS edge_id,c.name AS name' in query:
            return Result([dict(edge_id=r['edge_id'], name=r['source'], target=r['target'],
                rel_type=r['properties']['type'], raw_type=r['properties'].get('raw_type'),
                source_chapter_id=r['properties']['source_chapter_id'], source_hash=r['properties']['source_hash'])
                for r in self.edges if r['project_id'] == p['pid'] and r['source'] in p['names']
                and r['properties'].get('origin') != 'author' and r['properties'].get('source_chapter_id')
                and r['properties'].get('source_hash') and r['properties'].get('record_id') is None])
        if 'SET r.record_id=row.record_id' in query:
            for value in p['rows']:
                for row in self.edges:
                    if row['edge_id'] == value['edge_id'] and row['project_id'] == p['pid']:
                        row['properties']['record_id'] = value['record_id']
            return Result()
        if 'UNWIND $rows AS row' in query:
            if 'RELATES_TO' in query:
                for value in p['rows']:
                    if value.get('suppression_key') in self.suppressions: continue
                    found = any(row['project_id'] == p['pid'] and row['source'] == value['source']
                        and row['target'] == value['target'] and row['properties'].get('type') == value['type']
                        and row['properties'].get('chapter_start') == 0 for row in self.edges)
                    if not found:
                        self.edges.append(dict(edge_id=str(uuid4()), project_id=p['pid'], source=value['source'],
                            target=value['target'], properties=dict(type=value['type'], raw_type=value['raw_type'],
                                chapter_start=0, source_outline_id=p['oid'], source_outline_digest=p['digest'])))
            return Result()
        if 'MERGE (m:ChapterEntityProjection' in query:
            key = (p['pid'], p['chapter'])
            marker = self.markers.setdefault(key, dict(token=0, source_hash=None, payload_hash=None, schema_version=None))
            if marker['token'] > p['token']: return Result()
            marker['token'] = p['token']
            return Result([marker])
        if 'MATCH (m:ChapterEntityProjection' in query:
            marker = self.markers[(p['pid'], p['chapter'])]
            marker.update(source_hash=p.get('hash'), payload_hash=p.get('payload_hash'),
                status='applied' if 'version' in p else 'invalidated',
                schema_version=p.get('version', marker.get('schema_version')), global_idx=p.get('idx'))
            return Result()
        if 'RETURN DISTINCT c.name AS name' in query:
            return Result([dict(name=r['source']) for r in self.edges
                if r['project_id'] == p['pid'] and r['properties'].get('source_chapter_id') == p['chapter']])
        if 'WHERE r.source_chapter_id=$chapter DELETE r' in query:
            self.edges = [r for r in self.edges if not (r['project_id'] == p['pid']
                and r['properties'].get('source_chapter_id') == p['chapter'])]
            return Result()
        if 'MERGE (a)-[r:RELATES_TO {project_id:$pid' in query:
            if ('RelationshipSuppression' in query and p.get('suppression') in self.suppressions): return Result()
            matches = [r for r in self.edges if r['project_id'] == p['pid'] and r['source'] == p['c1']
                and r['target'] == p['c2'] and r['properties'].get('type') == p['kind']
                and r['properties'].get('chapter_start') == p['idx']]
            if 'manual.origin="author"' in query and any(r['properties'].get('origin') == 'author' for r in matches): return Result()
            if matches: row = matches[0]
            else:
                row = dict(edge_id=str(uuid4()), project_id=p['pid'], source=p['c1'], target=p['c2'], properties={})
                self.edges.append(row)
            row['properties'].update(type=p['kind'], chapter_start=p['idx'], source_chapter_id=p['chapter_id'],
                source_hash=p['source_hash'], event_kind='new', raw_type=p['kind'],
                record_id=p['record_id'], evidence=p['evidence'], replaces=None)
            return Result()
        if 'SET r.event_kind="change"' in query:
            for row in self.edges:
                props = row['properties']
                if (row['project_id'] == p['pid'] and row['source'] == p['c1'] and row['target'] == p['c2']
                        and props.get('source_chapter_id') == p['chapter_id'] and props.get('source_hash') == p['source_hash']
                        and props['type'] == p['kind']):
                    props.update(event_kind='change', replaces=deepcopy(p['replaces']))
            return Result()
        if 'collect(r) AS edges UNWIND edges AS r' in query:
            assert 'other.event_kind="change" OR other.type=r.type' not in query
            assert 'r.record_id IN coalesce(other.replaces,[])' in query
            edges = [r for r in self.edges if r['project_id'] == p['pid'] and r['source'] in p['names']
                and ('coalesce(r.origin,"")<>"author"' not in query or r['properties'].get('origin') != 'author')]
            for row in edges:
                props = row['properties']
                later = [other['properties']['chapter_start'] for other in edges
                    if other['source'] == row['source'] and other['target'] == row['target']
                    and other['properties']['chapter_start'] > props['chapter_start']
                    and ((other['properties']['type'] == props['type']
                          and (other['properties'].get('raw_type') or other['properties']['type'])
                          == (props.get('raw_type') or props['type']))
                         or (other['properties'].get('event_kind') == 'change'
                             and props.get('record_id') in (other['properties'].get('replaces') or [])))]
                if not props.get('source_chapter_id') and not props.get('projection_managed'):
                    props.update(projection_managed=True, projection_base_end=props.get('chapter_end'))
                terminal = props.get('projection_base_end') if not props.get('source_chapter_id') else None
                if terminal is not None: later.append(terminal + 1)
                props['chapter_end'] = min(later) - 1 if later else None
            return Result()
        if 'n.name AS target,e.type AS rel_type' in query:
            rows = []
            for edge in self.edges:
                props = edge['properties']
                if edge['project_id'] != p['pid']: continue
                if 'pairs' in p and (edge['source'], edge['target']) not in {tuple(pair) for pair in p['pairs']}: continue
                if 'coalesce(e.origin,"")<>"author"' in query and props.get('origin') == 'author': continue
                derived = props.get('source_chapter_id') and props.get('chapter_start', 0) < p['idx']
                author = 'e.origin="author"' in query and props.get('origin') == 'author'
                if not (derived or author): continue
                rows.append(dict(props, name=edge['source'], target=edge['target'], rel_type=props['type'],
                    global_idx=props.get('chapter_start'), chapter_end=props.get('chapter_end')))
            return Result(rows)
        if any(token in query for token in ('HAS_STATE', 'AT_LOCATION')):
            return Result()
        if 'MERGE (c:Character' in query or 'MERGE (:Character' in query:
            return Result()
        raise AssertionError(f'unhandled graph query: {query}')
