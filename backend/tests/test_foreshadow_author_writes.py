import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api import foreshadows as api
from app.models.project import Project, Foreshadow, ForeshadowWriteReceipt as Receipt, ForeshadowReview
from app.services import foreshadow_writes as writes, foreshadow_write_recovery as recovery
from app.services.foreshadow_graph_write import apply_graph_write
from app.services.foreshadow_provenance import foreshadow_source_hash
from app.services.foreshadow_write_contract import (
    make_write_command, ForeshadowWriteConflict, ForeshadowProjectUnavailable,
    ForeshadowSyncPending, ForeshadowWriteUnconfirmed,
)
from tests.foreshadow_graph_double import WriteGraph
from tests.test_foreshadow_review_store import database


@pytest.fixture
def runtime(database, monkeypatch):
    session, _, review, _ = database
    engine = session.get_bind()
    Receipt.__table__.create(engine)
    row = dict(session.execute(select(*Foreshadow.__table__.columns)).mappings().one())
    session.commit()
    r = NS(session=session, engine=engine, row=row, pid=row['project_id'], fid=row['id'],
        review=review, queries=[], fail_commit=False, fail_projection_commit=False,
        lose_sql_ack=False, commits=0, graph=WriteGraph())
    r.graph.nodes[(str(r.pid), str(r.fid))] = {**row,
        'resolve_conditions_json': json.dumps(row['resolve_conditions_json']),
        'resolution_blueprint_json': json.dumps(row['resolution_blueprint_json'])}
    def adapter(store):
        async def execute(statement):
            r.queries.append(statement)
            return store.execute(statement)
        async def commit():
            if r.fail_commit or (r.fail_projection_commit and r.graph.writes):
                r.fail_commit = r.fail_projection_commit = False
                raise RuntimeError('private database details must not escape')
            store.commit(); r.commits += 1
            if r.lose_sql_ack:
                r.lose_sql_ack = False
                raise ConnectionError('lost SQL commit acknowledgement')
        async def rollback(): store.rollback()
        return NS(execute=execute, commit=commit, rollback=rollback)
    r.db = adapter(session)
    @asynccontextmanager
    async def sessions():
        with Session(engine) as store:
            yield adapter(store)
    r.sessions = sessions
    r.mutex = AsyncMock()
    monkeypatch.setattr(writes, 'require_materialization_lock', r.mutex)
    monkeypatch.setattr(recovery, 'require_materialization_lock', r.mutex)
    return r


def command(r, operation='update', changes=None, *, version=None, rid=None, fid=None):
    return make_write_command(rid or uuid4(), r.pid, fid or r.fid, operation,
        version or foreshadow_source_hash(r.row), changes if changes is not None else {'description': '作者补写的寄信缘由'})


def graph_owned(r):
    r.session.execute(update(Foreshadow).values(source='neo4j'))
    r.session.commit()
    r.row['source'] = 'neo4j'


def current(r):
    row = r.session.execute(select(*Foreshadow.__table__.columns).where(Foreshadow.id == r.fid)).mappings().one_or_none()
    r.session.commit()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_local_edit_commits_before_ack_and_old_retry_cannot_overwrite_a_later_edit(runtime):
    r = runtime; cmd = command(r)
    result = await writes.apply_author_write(r.db, None, cmd)
    assert result['description'] == '作者补写的寄信缘由' and r.commits == 1
    assert r.session.execute(select(Receipt.outcome)).scalar_one() == 'applied'
    r.session.execute(update(Foreshadow).values(description='后来重新改过的缘由')); r.session.commit()
    retried = await writes.apply_author_write(r.db, None, cmd)
    assert retried['description'] == '后来重新改过的缘由'
    assert r.graph.calls == []
    assert current(r)['description'] == '后来重新改过的缘由'
    sql = [str(q.compile(dialect=postgresql.dialect())) for q in r.queries]
    assert any('FOR SHARE' in q and 'projects.id' in q for q in sql)
    assert any('FOR UPDATE' in q and 'foreshadows.project_id' in q for q in sql)


@pytest.mark.asyncio
async def test_local_stale_edit_is_rejected_durably_even_after_source_changes_back(runtime):
    r = runtime; cmd = command(r)
    r.session.execute(update(Foreshadow).values(description='其他作者的新线索')); r.session.commit()
    with pytest.raises(ForeshadowWriteConflict, match='source_changed'):
        await writes.apply_author_write(r.db, None, cmd)
    r.session.execute(update(Foreshadow).values(description=r.row['description'])); r.session.commit()
    with pytest.raises(ForeshadowWriteConflict, match='source_changed'):
        await writes.apply_author_write(r.db, None, cmd)
    assert current(r)['description'] == r.row['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_ack', [False, True])
async def test_sql_failure_never_reports_success_and_retry_has_one_effect(runtime, lost_ack):
    r = runtime; cmd = command(r)
    r.fail_commit, r.lose_sql_ack = not lost_ack, lost_ack
    with pytest.raises(ForeshadowWriteUnconfirmed): await writes.apply_author_write(r.db, None, cmd)
    assert current(r)['description'] == (cmd.changes['description'] if lost_ack else r.row['description'])
    result = await writes.apply_author_write(r.db, None, cmd)
    assert result['description'] == cmd.changes['description']
    assert len(r.session.execute(select(Receipt.id)).all()) == 1


@pytest.mark.asyncio
async def test_local_delete_retains_receipt_and_review_and_retries_without_graph(runtime):
    from app.services.foreshadow_review_store import persist_resolution_review
    r = runtime
    await persist_resolution_review(r.db, r.review); r.session.commit()
    cmd = command(r, 'delete', {})
    assert await writes.apply_author_write(r.db, None, cmd) is None
    assert await writes.apply_author_write(r.db, None, cmd) is None
    assert current(r) is None
    assert len(r.session.execute(select(Receipt.id)).all()) == 1
    assert len(r.session.execute(select(ForeshadowReview.id)).all()) == 1
    assert r.graph.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{}, {'status': 'planned', 'resolved_chapter': None},
    {'status': 'resolved', 'resolved_chapter': 920}, {'resolve_conditions_json': [], 'resolution_blueprint_json': None}])
async def test_local_partial_edits_preserve_fields_and_support_explicit_clearing(runtime, changes):
    r = runtime
    result = await writes.apply_author_write(r.db, None, command(r, changes=changes))
    for key, value in changes.items():
        assert result[key] == ({} if key == 'resolution_blueprint_json' and value is None else value)
    assert result['description'] == r.row['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['missing', 'deleted', 'unknown_source'])
async def test_unavailable_project_and_unknown_ownership_never_mutate_graph(runtime, unavailable):
    r = runtime
    if unavailable == 'deleted':
        from app.models.project import _utcnow
        r.session.execute(update(Project).values(deleted_at=_utcnow()))
    elif unavailable == 'unknown_source': r.session.execute(update(Foreshadow).values(source=None))
    r.session.commit()
    cmd = command(r)
    if unavailable == 'missing':
        from dataclasses import replace
        cmd = replace(cmd, project_id=uuid4())
    with pytest.raises((ForeshadowWriteConflict, ForeshadowProjectUnavailable)):
        await writes.apply_author_write(r.db, r.graph, cmd)
    assert r.graph.calls == []
    assert current(r)['description'] == r.row['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'update', 'resolve', 'delete'])
async def test_graph_crud_projects_the_actual_current_record_and_commits_receipt(runtime, operation):
    r = runtime; graph_owned(r)
    if operation == 'create':
        rid = uuid4()
        changes = {key: r.row[key] for key in ('type', 'description', 'planted_chapter',
            'resolve_conditions_json', 'resolution_blueprint_json', 'status', 'resolved_chapter', 'narrative_proximity')}
        cmd = make_write_command(rid, r.pid, rid, 'create', None, changes)
    elif operation == 'resolve': cmd = command(r, operation, {'status': 'resolved', 'resolved_chapter': 920, 'narrative_proximity': 1})
    elif operation == 'delete': cmd = command(r, operation, {})
    else: cmd = command(r)
    result = await writes.apply_author_write(r.db, r.graph, cmd)
    assert result is None if operation == 'delete' else result['id'] == cmd.foreshadow_id
    receipt = r.graph.receipts[str(cmd.id)]
    assert receipt['sync_pending'] is False and receipt['outcome'] == 'applied'
    assert r.graph.writes == 1 and r.commits == 2
    await writes.apply_author_write(r.db, r.graph, cmd)
    assert r.graph.writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['sql_commit', 'graph_ack', 'sync_ack'])
async def test_graph_write_recovery_projects_later_author_values_without_replaying_old_edit(runtime, failure):
    r = runtime; graph_owned(r); cmd = command(r)
    r.fail_projection_commit = failure == 'sql_commit'
    r.graph.lose_commit_ack = failure == 'graph_ack'
    r.graph.fail_ack = failure == 'sync_ack'
    if failure == 'sync_ack': await writes.apply_author_write(r.db, r.graph, cmd)
    else:
        with pytest.raises((ForeshadowSyncPending, ForeshadowWriteUnconfirmed)):
            await writes.apply_author_write(r.db, r.graph, cmd)
    assert r.graph.receipts[str(cmd.id)]['sync_pending'] is True
    r.graph.nodes[(str(r.pid), str(r.fid))]['description'] = '图谱里作者后来的修改'
    r.graph.fail_ack = False
    assert await recovery.replay_foreshadow_writes(r.graph, sessions=r.sessions) == {'examined': 1, 'completed': 1, 'failed': 0}
    assert current(r)['description'] == '图谱里作者后来的修改'
    assert r.graph.writes == 1
    assert (await writes.apply_author_write(r.db, r.graph, cmd))['description'] == '图谱里作者后来的修改'
    assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_deleted_graph_record_is_cleaned_up_by_receipt_recovery_without_resurrection(runtime):
    r = runtime; graph_owned(r); cmd = command(r, 'delete', {})
    r.fail_projection_commit = True
    with pytest.raises(ForeshadowSyncPending): await writes.apply_author_write(r.db, r.graph, cmd)
    assert current(r) is not None
    assert (await recovery.replay_foreshadow_writes(r.graph, sessions=r.sessions))['completed'] == 1
    assert current(r) is None
    assert await writes.apply_author_write(r.db, r.graph, cmd) is None
    assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_graph_conflict_refreshes_projection_but_does_not_apply_stale_patch(runtime):
    r = runtime; graph_owned(r); cmd = command(r)
    r.graph.nodes[(str(r.pid), str(r.fid))]['description'] = '另一个作者修改的事实'
    with pytest.raises(ForeshadowWriteConflict, match='source_changed'):
        await writes.apply_author_write(r.db, r.graph, cmd)
    assert current(r)['description'] == '另一个作者修改的事实'
    assert r.graph.writes == 0


@pytest.mark.asyncio
async def test_recovery_failure_defers_receipt_and_does_not_replace_local_owner(runtime):
    r = runtime; graph_owned(r); cmd = command(r)
    r.graph.lose_commit_ack = True
    with pytest.raises(ForeshadowWriteUnconfirmed): await writes.apply_author_write(r.db, r.graph, cmd)
    r.session.execute(update(Foreshadow).values(source='lifecycle', description='新的本地来源')); r.session.commit()
    assert await recovery.replay_foreshadow_writes(r.graph, sessions=r.sessions) == {'examined': 1, 'completed': 0, 'failed': 1}
    receipt = r.graph.receipts[str(cmd.id)]
    assert receipt['sync_pending'] is True and receipt['sync_after'] > r.graph.now
    assert current(r)['description'] == '新的本地来源'


@pytest.mark.asyncio
async def test_duplicate_graph_requests_are_atomic_and_receipt_failure_rolls_back(runtime):
    r = runtime; graph_owned(r); cmd = command(r)
    r.graph.fail_receipt = True
    with pytest.raises(RuntimeError): await apply_graph_write(r.graph, cmd)
    assert r.graph.writes == 0 and r.graph.receipts == {}
    r.graph.fail_receipt = False
    results = await asyncio.gather(apply_graph_write(r.graph, cmd), apply_graph_write(r.graph, cmd))
    assert results[0] == results[1] and r.graph.writes == 1
    with pytest.raises(ForeshadowWriteConflict, match='request_id_reused'):
        await apply_graph_write(r.graph, command(r, changes={'description': '改过的请求'}, rid=cmd.id))
    assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_http_requires_version_and_idempotency_and_only_returns_committed_data(runtime):
    r = runtime
    app = FastAPI(); app.include_router(api.router)
    async def provide_db(): return r.db
    async def provide_graph(): return r.graph
    app.dependency_overrides[api.get_db] = provide_db
    app.dependency_overrides[api.get_foreshadow_driver] = provide_graph
    url = f'/api/projects/{r.pid}/foreshadows/{r.fid}'
    headers = {'Idempotency-Key': str(uuid4()), 'If-Match': foreshadow_source_hash(r.row)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.put(url, json={'description': '新悬念'})).status_code == 422
        assert r.commits == 0 and r.graph.calls == []
        saved = await client.put(url, json={'description': '新悬念'}, headers=headers)
        assert saved.status_code == 200 and r.commits == 1
        assert saved.json()['description'] == '新悬念' and len(saved.json()['version']) == 64
        r.fail_commit = True
        failed = await client.put(url, json={'description': '待保存稿'}, headers={
            'Idempotency-Key': str(uuid4()), 'If-Match': saved.json()['version']})
        assert failed.status_code == 503 and failed.json()['detail']['code'] == 'foreshadow_write_unconfirmed'
        assert 'private' not in failed.text
        assert current(r)['description'] == '新悬念'


@pytest.mark.asyncio
async def test_http_create_keeps_identity_on_retry_and_never_adds_duplicate(runtime):
    r = runtime; rid = uuid4()
    app = FastAPI(); app.include_router(api.router)
    async def provide_db(): return r.db
    async def provide_graph(): return r.graph
    app.dependency_overrides[api.get_db] = provide_db
    app.dependency_overrides[api.get_foreshadow_driver] = provide_graph
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        payload = {'type': 'mystery', 'description': '一封信', 'planted_chapter': 3}
        headers = {'Idempotency-Key': str(rid)}
        first = await client.post(f'/api/projects/{r.pid}/foreshadows', json=payload, headers=headers)
        again = await client.post(f'/api/projects/{r.pid}/foreshadows', json=payload, headers=headers)
        assert first.status_code == again.status_code == 201
        assert first.json()['id'] == again.json()['id'] == str(rid)
        assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_competing_graph_edits_compare_under_the_write_lock(runtime):
    r = runtime; graph_owned(r)
    first = command(r, changes={'description': '作者甲的版本'})
    second = command(r, changes={'description': '作者乙的版本'})
    results = await asyncio.gather(apply_graph_write(r.graph, first), apply_graph_write(r.graph, second))
    assert sorted(result['outcome'] for result in results) == ['applied', 'rejected']
    assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_rejected_graph_request_stays_rejected_after_source_is_restored(runtime):
    r = runtime; graph_owned(r); cmd = command(r)
    node = r.graph.nodes[(str(r.pid), str(r.fid))]
    previous = deepcopy(node)
    node['description'] = '已变化的事实'
    assert (await apply_graph_write(r.graph, cmd))['outcome'] == 'rejected'
    r.graph.nodes[(str(r.pid), str(r.fid))] = previous
    assert (await apply_graph_write(r.graph, cmd))['outcome'] == 'rejected'
    assert r.graph.writes == 0


@pytest.mark.asyncio
async def test_create_cannot_use_another_projects_existing_foreshadow_id(runtime):
    r = runtime; other = uuid4()
    r.session.execute(insert(Project).values(id=other, title='其他作品')); r.session.commit()
    cmd = make_write_command(r.fid, other, r.fid, 'create', None, {})
    with pytest.raises(ForeshadowWriteConflict, match='already_exists'):
        await writes.apply_author_write(r.db, r.graph, cmd)
    assert r.graph.calls == [] and current(r)['project_id'] == r.pid


@pytest.mark.asyncio
async def test_retry_of_old_creation_never_recreates_a_subsequently_deleted_clue(runtime):
    r = runtime; rid = uuid4()
    changes = {key: r.row[key] for key in ('type', 'description', 'planted_chapter',
        'resolve_conditions_json', 'resolution_blueprint_json', 'status', 'resolved_chapter', 'narrative_proximity')}
    create = make_write_command(rid, r.pid, rid, 'create', None, changes)
    created = await writes.apply_author_write(r.db, r.graph, create)
    remove = make_write_command(uuid4(), r.pid, rid, 'delete', foreshadow_source_hash(created), {})
    await writes.apply_author_write(r.db, r.graph, remove)
    with pytest.raises(ForeshadowWriteConflict, match='source_missing'):
        await writes.apply_author_write(r.db, r.graph, create)
    assert r.graph.writes == 2 and (str(r.pid), str(rid)) not in r.graph.nodes


@pytest.mark.asyncio
async def test_uncertain_graph_request_cannot_be_rebound_to_a_local_clue(runtime):
    r = runtime; graph_owned(r); original = command(r)
    r.graph.lose_commit_ack = True
    with pytest.raises(ForeshadowWriteUnconfirmed):
        await writes.apply_author_write(r.db, r.graph, original)
    local_id = uuid4()
    r.session.execute(insert(Foreshadow).values(**{**r.row, 'id': local_id, 'source': 'lifecycle'}))
    local = dict(r.session.execute(select(*Foreshadow.__table__.columns).where(Foreshadow.id == local_id)).mappings().one())
    r.session.commit()
    wrong = make_write_command(original.id, r.pid, local_id, 'update', foreshadow_source_hash(local), {'description': '不应改到另一条伏笔'})
    with pytest.raises(ForeshadowWriteConflict, match='request_id_reused'):
        await writes.apply_author_write(r.db, r.graph, wrong)
    assert r.session.execute(select(Foreshadow.description).where(Foreshadow.id == local_id)).scalar_one() == local['description']
    r.session.commit()
    assert (await recovery.replay_foreshadow_writes(r.graph, sessions=r.sessions))['completed'] == 1


@pytest.mark.asyncio
async def test_graph_request_binding_must_commit_before_any_graph_write(runtime):
    r = runtime; graph_owned(r); r.fail_commit = True
    with pytest.raises(ForeshadowWriteUnconfirmed):
        await writes.apply_author_write(r.db, r.graph, command(r))
    assert r.graph.calls == []
    assert r.session.execute(select(Receipt.id)).first() is None


@pytest.mark.asyncio
async def test_unstarted_graph_request_preserves_identity_for_explicit_retry(runtime, monkeypatch):
    r = runtime; graph_owned(r); cmd = command(r)
    original = writes.apply_graph_write
    failing = AsyncMock(side_effect=ConnectionError('graph unavailable'))
    monkeypatch.setattr(writes, 'apply_graph_write', failing)
    with pytest.raises(ForeshadowWriteUnconfirmed):
        await writes.apply_author_write(r.db, r.graph, cmd)
    assert r.session.execute(select(Receipt.outcome, Receipt.command_sha256)).one() == ('pending', cmd.sha256)
    r.session.commit()
    assert await recovery.replay_foreshadow_writes(r.graph, sessions=r.sessions) == {'examined': 0, 'completed': 0, 'failed': 0}
    monkeypatch.setattr(writes, 'apply_graph_write', original)
    assert (await writes.apply_author_write(r.db, r.graph, cmd))['description'] == cmd.changes['description']
    assert r.graph.writes == 1


@pytest.mark.asyncio
async def test_local_http_edit_is_available_without_an_initialized_graph_driver(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr(api.neo4j_db, '_driver', None)
    app = FastAPI(); app.include_router(api.router)
    async def provide_db(): return r.db
    app.dependency_overrides[api.get_db] = provide_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.put(f'/api/projects/{r.pid}/foreshadows/{r.fid}', json={'description': '本地修改'},
            headers={'Idempotency-Key': str(uuid4()), 'If-Match': foreshadow_source_hash(r.row)})
    assert response.status_code == 200 and response.json()['description'] == '本地修改'
    assert r.graph.calls == []


def test_receipt_migration_retains_retry_proof_and_cascades_only_on_project_purge():
    from datetime import datetime, timezone
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    path = Path(__file__).parents[1] / 'alembic/versions/a1001941_foreshadow_write_receipts.py'
    spec = importlib.util.spec_from_file_location('write_receipt_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine = create_engine('sqlite://')
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            Project.__table__.create(connection)
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert {column['name'] for column in inspect(connection).get_columns(Receipt.__tablename__)} == set(Receipt.__table__.columns.keys())
            pid = uuid4()
            connection.execute(insert(Project).values(id=pid, title='迁移测试'))
            connection.execute(insert(Receipt).values(id=uuid4(), project_id=pid, foreshadow_id=uuid4(),
                command_sha256='a' * 64, operation='delete', source='lifecycle', outcome='applied',
                created_at=datetime.now(timezone.utc)))
            with pytest.raises(RuntimeError, match='Preserve author write receipts'):
                module.downgrade()
            connection.execute(delete(Project).where(Project.id == pid))
            assert connection.execute(select(Receipt.id)).first() is None
            module.downgrade()
            assert Receipt.__tablename__ not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
