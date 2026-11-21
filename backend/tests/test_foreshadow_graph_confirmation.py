import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from app.services.foreshadow_graph_confirmation import decide_graph_confirmation, projected_foreshadow, GraphConfirmationInvalid


class Result:
    def __init__(self, row=None): self.row = row
    async def single(self): return deepcopy(self.row)
    async def consume(self): pass


class Graph:
    """Transactional contract double; not evidence of actual Neo4j locking."""
    def __init__(self, node):
        self.node = deepcopy(node); self.receipt = None; self.calls = []
        self.writes = 0; self.fail_receipt = False; self.lose_commit_ack = False
        self.lock = asyncio.Lock()
    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def execute_write(self, callback):
        async with self.lock:
            before = deepcopy((self.node, self.receipt, self.writes))
            try: result = await callback(self)
            except BaseException:
                self.node, self.receipt, self.writes = before
                raise
            if self.lose_commit_ack:
                self.lose_commit_ack = False
                raise ConnectionError('lost commit acknowledgement')
            return result
    async def run(self, query, **p):
        self.calls.append((query, p))
        if query.startswith('CREATE CONSTRAINT'): return Result()
        if query.startswith('MERGE (r:ForeshadowConfirmation'):
            assert 'write_lock=coalesce' in query
            if self.receipt is None:
                self.receipt = dict(id=p['rid'], project_id=p['pid'], foreshadow_id=p['fid'], snapshot_sha256=p['digest'])
            self.receipt['write_lock'] = self.receipt.get('write_lock', 0) + 1
            return Result({'receipt': self.receipt})
        if 'RETURN properties(f) AS node' in query:
            return Result({'node': self.node}) if self.node is not None else Result()
        if 'SET f.status="resolved"' in query:
            self.node.update(status='resolved', resolved_chapter=p['chapter'], narrative_proximity=1.0)
            self.writes += 1
            return Result()
        if 'SET r.outcome=' in query:
            if self.fail_receipt: raise RuntimeError('receipt write failed')
            self.receipt.update(outcome=p['outcome'], decided_at=1791446400000)
            return Result({'receipt': self.receipt})
        raise AssertionError(query)


@pytest.fixture
def command():
    node = dict(type='mystery', description='寄信人', planted_chapter=2,
        resolve_conditions_json='["身份明确"]', resolution_blueprint_json='{}',
        status='ready', resolved_chapter=None)
    return Graph(node), dict(review_id=uuid4(), project_id=uuid4(), foreshadow_id=uuid4(),
        snapshot_sha256='a' * 64, expected=projected_foreshadow(node), global_idx=10, allow_apply=True)


@pytest.mark.asyncio
async def test_concurrent_retries_mutate_once_and_keep_durable_receipt_after_deletion(command):
    graph, args = command
    results = await asyncio.gather(decide_graph_confirmation(graph, **args), decide_graph_confirmation(graph, **args))
    assert graph.writes == 1 and results[0] == results[1]
    graph.node = None
    assert (await decide_graph_confirmation(graph, **{**args, 'allow_apply': False})) == results[0]
    assert graph.node is None and graph.writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['chapter', 'description', 'deleted', 'malformed', 'resolved'])
async def test_rejected_receipt_fences_a_late_stale_attempt(command, conflict):
    graph, args = command; before = deepcopy(graph.node)
    if conflict == 'chapter': args['allow_apply'] = False
    if conflict == 'description': graph.node['description'] = '新悬念'
    if conflict == 'deleted': graph.node = None
    if conflict == 'malformed': graph.node['resolve_conditions_json'] = 'broken'
    if conflict == 'resolved': graph.node['status'] = 'resolved'
    result = await decide_graph_confirmation(graph, **args)
    assert result['outcome'] == 'rejected' and graph.writes == 0
    graph.node = before
    assert (await decide_graph_confirmation(graph, **{**args, 'allow_apply': True}))['outcome'] == 'rejected'
    assert graph.writes == 0


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_story_change(command):
    graph, args = command; before = deepcopy(graph.node)
    graph.fail_receipt = True
    with pytest.raises(RuntimeError): await decide_graph_confirmation(graph, **args)
    assert graph.node == before and graph.receipt is None and graph.writes == 0


@pytest.mark.asyncio
async def test_lost_commit_ack_does_not_replay_over_later_author_edit(command):
    graph, args = command; graph.lose_commit_ack = True
    with pytest.raises(ConnectionError): await decide_graph_confirmation(graph, **args)
    graph.node.update(description='作者重新写过的悬念', status='planted', resolved_chapter=None)
    assert (await decide_graph_confirmation(graph, **args))['outcome'] == 'applied'
    assert graph.node['status'] == 'planted' and graph.writes == 1


@pytest.mark.asyncio
async def test_receipt_scope_and_snapshot_mismatch_never_writes(command):
    graph, args = command
    await decide_graph_confirmation(graph, **args)
    for field, value in [('project_id', uuid4()), ('foreshadow_id', uuid4()), ('snapshot_sha256', 'b' * 64)]:
        with pytest.raises(GraphConfirmationInvalid): await decide_graph_confirmation(graph, **{**args, field: value})
    assert graph.writes == 1


@pytest.mark.asyncio
async def test_planted_graph_clue_can_be_confirmed_from_prose_without_score_promotion(command):
    graph, args = command
    graph.node['status'] = 'planted'
    args['expected'] = projected_foreshadow(graph.node)
    assert (await decide_graph_confirmation(graph, **args))['outcome'] == 'applied'
    assert graph.node['status'] == 'resolved' and graph.writes == 1
