from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.api import foreshadows as api
from app.models.project import Foreshadow
from app.services.foreshadow_provenance import foreshadow_source_hash
from tests.test_foreshadow_author_writes import runtime, current, graph_owned
from tests.test_foreshadow_review_store import database


@pytest.mark.asyncio
async def test_edit_lifecycle_plan_uses_postgres_and_maps_fields(runtime):
    r = runtime
    result = await api.update_foreshadow(r.pid, r.fid,
        api.ForeshadowUpdateRequest(description='修改后的印记', resolve_conditions=['辨认出笔迹']),
        None, r.db, uuid4(), foreshadow_source_hash(r.row))
    assert result.description == '修改后的印记'
    assert result.resolve_conditions_json == ['辨认出笔迹']
    assert result.status == r.row['status'] and result.source == 'lifecycle'
    assert r.commits == 1 and r.graph.calls == []


@pytest.mark.asyncio
async def test_delete_lifecycle_plan_uses_postgres(runtime):
    r = runtime
    await api.delete_foreshadow(r.pid, r.fid, None, r.db, uuid4(), foreshadow_source_hash(r.row))
    assert current(r) is None
    assert r.commits == 1 and r.graph.calls == []


@pytest.mark.asyncio
async def test_explicit_author_resolution_does_not_require_graph_materialization(runtime):
    r = runtime
    result = await api.resolve_foreshadow(r.pid, r.fid, api.ForeshadowResolveRequest(resolved_chapter=920),
        None, r.db, uuid4(), foreshadow_source_hash(r.row))
    assert result.status == 'resolved' and result.resolved_chapter == 920
    assert result.narrative_proximity == 1
    assert r.graph.calls == []
    with pytest.raises(HTTPException) as caught:
        await api.resolve_foreshadow(r.pid, r.fid, api.ForeshadowResolveRequest(resolved_chapter=921),
            None, r.db, uuid4(), result.version)
    assert caught.value.status_code == 409
    assert current(r)['resolved_chapter'] == 920


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['update', 'resolve', 'delete'])
async def test_graph_owned_records_keep_graph_write_and_scoped_projection(runtime, action):
    r = runtime; graph_owned(r)
    args = r.graph, r.db, uuid4(), foreshadow_source_hash(r.row)
    if action == 'update':
        result = await api.update_foreshadow(r.pid, r.fid, api.ForeshadowUpdateRequest(description='新描述'), *args)
        assert result.description == '新描述'
    elif action == 'resolve':
        result = await api.resolve_foreshadow(r.pid, r.fid, api.ForeshadowResolveRequest(resolved_chapter=920), *args)
        assert result.status == 'resolved' and result.resolved_chapter == 920
    else:
        await api.delete_foreshadow(r.pid, r.fid, *args)
        assert current(r) is None
    assert r.graph.writes == 1 and r.commits == 2
    for query, params in r.graph.calls:
        if 'pid' in params: assert params['pid'] == str(r.pid)
        if 'fid' in params: assert params['fid'] == str(r.fid)


@pytest.mark.asyncio
async def test_failed_local_commit_is_not_rerouted_to_graph(runtime):
    r = runtime; r.fail_commit = True
    with pytest.raises(HTTPException) as caught:
        await api.update_foreshadow(r.pid, r.fid, api.ForeshadowUpdateRequest(description='候选改动'),
            r.graph, r.db, uuid4(), foreshadow_source_hash(r.row))
    assert caught.value.status_code == 503
    assert r.graph.calls == [] and current(r)['description'] == r.row['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['lifecycle', 'neo4j'])
@pytest.mark.parametrize('status, chapter', [('planned', None), ('resolved', 920)])
async def test_status_patch_writes_resolution_pair_to_owning_store(runtime, source, status, chapter):
    r = runtime
    r.row.update(source=source, status='resolved', resolved_chapter=915)
    r.session.execute(update(Foreshadow).values(source=source, status='resolved', resolved_chapter=915)); r.session.commit()
    r.graph.nodes[(str(r.pid), str(r.fid))].update(status='resolved', resolved_chapter=915)
    patch = {'status': status}
    if status == 'resolved': patch['resolved_chapter'] = chapter
    result = await api.update_foreshadow(r.pid, r.fid, api.ForeshadowUpdateRequest(**patch),
        r.graph, r.db, uuid4(), foreshadow_source_hash(r.row))
    assert result.status == status and result.resolved_chapter == chapter
    if source == 'neo4j': assert r.graph.writes == 1
    else: assert r.graph.calls == []
