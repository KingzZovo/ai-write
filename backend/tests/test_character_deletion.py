import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import json

import pytest

from app.services.character_deletion import delete_character_graph, stage_character_deletions, replay_character_deletions
from app.services.character_profile_write import CharacterProfileConflict


class Result:
    def __init__(self, rows=()): self.rows = list(rows)
    async def single(self): return self.rows[0] if self.rows else None
    async def data(self): return self.rows
    async def consume(self): pass


class Driver:
    def __init__(self):
        self.character = {'profile': json.dumps({'identity': '师父'}), 'states': 2}
        self.receipts = {}
        self.fail_character_delete = False
        self.relationship_locks = 0
        self.queries = []
    def session(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def execute_write(self, callback):
        before = deepcopy((self.character, self.receipts))
        try: return await callback(self)
        except BaseException:
            self.character, self.receipts = before
            raise
    async def run(self, query, **params):
        self.queries.append(query)
        if query.startswith('CREATE CONSTRAINT'): return Result()
        if 'MERGE (g:RelationshipProjectGuard' in query:
            self.relationship_locks += 1
            return Result()
        if query.startswith('MERGE (d:'):
            self.pid = params['pid']
            row = self.receipts.setdefault(params['token'], {'name': params['name'], 'status': 'preparing'})
            return Result([deepcopy(row)])
        if 'RETURN c.profile_json AS profile' in query:
            return Result([deepcopy(self.character)] if self.character else [])
        if 'DETACH DELETE s' in query:
            self.character['states'] = 0
            return Result()
        if 'DETACH DELETE c' in query:
            if self.fail_character_delete: raise RuntimeError('fail mid transaction')
            self.character = None
            return Result()
        if 'SET d.status="pending"' in query:
            self.receipts[params['token']]['status'] = 'pending'
            return Result()
        if 'RETURN d.token AS token' in query:
            return Result([{'token': token, 'name': row['name']} for token, row in self.receipts.items() if row['status'] == 'pending'])
        if 'collect(d.token)' in query:
            return Result([{'project_id': self.pid, 'tokens': list(self.receipts)}])
        if 'SET d.retry_after=' in query:
            self.deferred = params
            return Result()
        raise AssertionError(query)


@pytest.mark.asyncio
async def test_graph_delete_rolls_back_states_and_receipt_on_failure():
    driver = Driver()
    driver.fail_character_delete = True
    with pytest.raises(RuntimeError):
        await delete_character_graph(driver, uuid4(), '周砚', {'identity': '师父'}, uuid4())
    assert driver.character['states'] == 2
    assert driver.receipts == {}
    assert driver.relationship_locks == 1


@pytest.mark.asyncio
async def test_stale_delete_preserves_current_profile_and_states():
    driver = Driver()
    with pytest.raises(CharacterProfileConflict):
        await delete_character_graph(driver, uuid4(), '周砚', {'identity': '旧稿'}, uuid4())
    assert driver.character['states'] == 2 and driver.receipts == {}


@pytest.mark.asyncio
async def test_duplicate_delete_token_never_deletes_recreated_character():
    driver, pid, token = Driver(), uuid4(), uuid4()
    await delete_character_graph(driver, pid, '周砚', {'identity': '师父'}, token)
    assert driver.character is None and driver.receipts[str(token)]['status'] == 'pending'
    driver.character = {'profile': '{}', 'states': 5}
    driver.receipts[str(token)]['status'] = 'completed'
    await delete_character_graph(driver, pid, '周砚', {'identity': '师父'}, token)
    assert driver.character['states'] == 5


@pytest.mark.asyncio
async def test_staging_journals_cleanup_and_does_not_repeat_it_for_a_new_row():
    driver, pid, token = Driver(), uuid4(), uuid4()
    await delete_character_graph(driver, pid, '周砚', {'identity': '师父'}, token)
    applied, added = set(), []
    def add(row): added.append(row); applied.add(row.token)
    db = SimpleNamespace(scalars=AsyncMock(side_effect=lambda query: SimpleNamespace(all=lambda: list(applied))),
        execute=AsyncMock(), add=add, commit=AsyncMock())
    assert await stage_character_deletions(db, driver, pid) == [str(token)]
    assert db.execute.await_count == 4  # Character and three name-keyed satellites.
    for call in db.execute.call_args_list:
        values = list(call.args[0].compile().params.values())
        assert pid in values and '周砚' in values
    assert added[0].token == token and added[0].project_id == pid
    db.commit.assert_not_awaited()
    # PG commit succeeded, acknowledgement was lost, and a new same-name row
    # now exists. The journal prevents another round of DELETEs.
    await stage_character_deletions(db, driver, pid)
    assert db.execute.await_count == 4 and len(added) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_recovery_uses_strict_materializer_and_defers_only_selected_receipts(fail):
    driver, pid, token = Driver(), uuid4(), uuid4()
    await delete_character_graph(driver, pid, '周砚', {'identity': '师父'}, token)
    materialize = AsyncMock(side_effect=RuntimeError('busy') if fail else None)
    result = await replay_character_deletions(driver, materialize=materialize)
    assert result == {'projects': 1, 'completed': 0 if fail else 1, 'failed': 1 if fail else 0}
    assert materialize.call_args.kwargs['strict'] is True
    assert materialize.call_args.kwargs['graph_driver'] is driver
    if fail: assert driver.deferred == {'pid': str(pid), 'tokens': [str(token)]}


@pytest.mark.asyncio
async def test_projector_does_not_acknowledge_if_cleanup_commit_fails(monkeypatch):
    from app.tasks import entity_tasks
    events = []
    db = SimpleNamespace(scalar=AsyncMock(return_value=True), commit=AsyncMock(side_effect=RuntimeError('commit failed')),
                         rollback=AsyncMock())
    @asynccontextmanager
    async def sessions(): yield db
    monkeypatch.setattr('app.db.session.async_session_factory', sessions)
    async def stage(*args): events.append('cleanup'); return ['token']
    async def snapshot(*args, **kwargs): events.append('snapshot'); return {}
    acknowledge = AsyncMock()
    monkeypatch.setattr('app.services.character_deletion.stage_character_deletions', stage)
    monkeypatch.setattr('app.services.character_deletion.acknowledge_character_deletions', acknowledge)
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', snapshot)
    with pytest.raises(RuntimeError):
        await entity_tasks._materialize_entities_to_postgres(project_id=str(uuid4()), chapter_idx=0,
            caller='test', strict=True, graph_driver=object())
    assert events == ['cleanup', 'snapshot']
    acknowledge.assert_not_awaited()
    db.rollback.assert_awaited_once()


def test_deletion_task_uses_async_bridge(monkeypatch):
    from app import tasks
    from app.tasks import entity_tasks
    from app.db import neo4j
    driver = object()
    recovery = AsyncMock(return_value={'projects': 0})
    monkeypatch.setattr(neo4j, 'init_neo4j', AsyncMock())
    monkeypatch.setattr(neo4j, '_driver', driver)
    monkeypatch.setattr('app.services.character_deletion.replay_character_deletions', recovery)
    monkeypatch.setattr(tasks, '_run_async_safe', asyncio.run)
    assert entity_tasks.replay_character_deletions.run() == {'projects': 0}
    recovery.assert_awaited_once_with(driver)


@pytest.mark.asyncio
async def test_replay_query_is_valid_cypher_for_aggregation():
    """回归:RETURN 带 collect 时,ORDER BY 不能裸写 min(...)。

    实测 2026-11-27,原查询是
        RETURN d.project_id AS project_id,collect(d.token) AS tokens
        ORDER BY min(coalesce(d.retry_after,0)),project_id
    Neo4j 直接判 CypherSyntaxError("In a WITH/RETURN with DISTINCT or an aggregation,
    it is not possible to access variables declared before") —— 于是这个恢复任务
    每 60 秒抛一次错,角色删除的重放从来没跑成过。修法是把到期时间也投影成
    `AS due`,再 `ORDER BY due`。
    """
    driver, pid, token = Driver(), uuid4(), uuid4()
    await delete_character_graph(driver, pid, '周砚', {'identity': '师父'}, token)
    await replay_character_deletions(driver, materialize=AsyncMock())
    replay = [q for q in driver.queries if 'collect(d.token)' in q]
    assert replay, '没找到重放查询'
    query = replay[0]
    assert 'ORDER BY min(' not in query, 'ORDER BY 里裸用聚合,Neo4j 会判 CypherSyntaxError'
    assert 'min(coalesce(d.retry_after,0)) AS due' in query
    assert 'ORDER BY due' in query
