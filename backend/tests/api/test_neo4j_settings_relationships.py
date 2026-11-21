"""Authenticated relationship API with real SQLite writes and a graph boundary double."""
from uuid import uuid4

import pytest
import pytest_asyncio

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from app.main import app
from tests.test_relationship_author_writes import author_db, current
from tests.test_character_state_context import state_db


@pytest_asyncio.fixture
async def neo_env(author_db):
    r = author_db
    previous = dict(app.dependency_overrides)
    async def driver(): yield r.graph
    async def db(): yield r.db
    app.dependency_overrides[get_neo4j] = driver
    app.dependency_overrides[get_db] = db
    r.url = f'/api/projects/{r.pid}/neo4j-settings/relationships'
    r.identity = dict(source='陈川', target='沈青', rel_type='同伴')
    try:
        yield r
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def original(client, r, **identity):
    response = await client.get(r.url + '/record', params=r.identity | identity)
    assert response.status_code == 200, response.text
    return response.json()


async def headers(client, r, **identity):
    snapshot = await original(client, r, **identity)
    return {'Idempotency-Key': str(uuid4()), 'If-Match': snapshot['version']}


@pytest.mark.asyncio
async def test_put_updates_graph_and_committed_read_model_before_saved(auth_client, neo_env):
    r = neo_env
    request_headers = await headers(auth_client, r)
    response = await auth_client.put(r.url, headers=request_headers, json=r.identity | {
        'label': '暂时结伴', 'note': '仍需各自提防', 'sentiment': 'neutral'})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'saved'
    assert response.json()['request_id'] == request_headers['Idempotency-Key']
    assert current(r)['note'] == '仍需各自提防' and r.commits == 1
    assert r.graph.edges[0]['properties']['label'] == '暂时结伴'


@pytest.mark.asyncio
async def test_put_renames_rel_type_preserving_identity_and_history(auth_client, neo_env):
    r = neo_env
    response = await auth_client.put(r.url, headers=await headers(auth_client, r),
        json=r.identity | {'new_rel_type': '敌对（尚有共同目的）'})
    assert response.status_code == 202, response.text
    assert response.json()['rel_type'] == '敌对'
    row = current(r)
    assert row['id'] == r.rel_id and row['rel_type'] == '敌对' and row['evolution_json'] == r.evolution
    assert r.graph.edges[0]['properties']['raw_type'] == '敌对（尚有共同目的）'


@pytest.mark.asyncio
async def test_author_rename_no_longer_claims_to_be_unchanged_prose(auth_client, neo_env):
    r = neo_env
    response = await auth_client.put(r.url, headers=await headers(auth_client, r),
        json=r.identity | {'new_rel_type': '敌对'})
    assert response.status_code == 202, response.text
    props = r.graph.edges[0]['properties']
    assert props['origin'] == 'author'
    assert not props.get('source_chapter_id') and not props.get('source_hash')


@pytest.mark.asyncio
async def test_retry_of_old_author_request_cannot_restore_an_older_label(auth_client, neo_env):
    r = neo_env
    first = await headers(auth_client, r)
    body = r.identity | {'label': '暂时结伴'}
    assert (await auth_client.put(r.url, headers=first, json=body)).status_code == 202
    assert (await auth_client.put(r.url, headers=await headers(auth_client, r),
        json=r.identity | {'label': '作者后来的修订'})).status_code == 202
    response = await auth_client.put(r.url, headers=first, json=body)
    assert response.status_code == 202, response.text
    assert current(r)['label'] == '作者后来的修订' and r.graph.mutations == 2


@pytest.mark.asyncio
async def test_verbose_identity_matches_the_existing_canonical_group(auth_client, neo_env):
    r = neo_env
    response = await auth_client.put(r.url, headers=await headers(auth_client, r),
        json=r.identity | {'rel_type': '同伴（并肩作战）', 'label': '战友'})
    assert response.status_code == 202, response.text
    assert current(r)['label'] == '战友'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
async def test_missing_source_is_a_version_conflict_without_graph_mutation(auth_client, neo_env, method):
    r = neo_env
    identity = r.identity | {'target': '不存在的人'}
    body = identity | ({'label': '修改'} if method == 'PUT' else {})
    response = await auth_client.request(method, r.url, json=body,
        headers=await headers(auth_client, r, target=identity['target']))
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'source_missing' and r.graph.mutations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('use_body', [True, False])
async def test_delete_body_and_query_forms_remove_projection_only_after_graph_commit(auth_client, neo_env, use_body):
    r = neo_env
    request_headers = await headers(auth_client, r)
    options = {'json' if use_body else 'params': r.identity}
    response = await auth_client.request('DELETE', r.url, headers=request_headers, **options)
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'saved' and not r.graph.edges and current(r) is None
    assert r.graph.receipts[request_headers['Idempotency-Key']]['projection_before_json']


@pytest.mark.asyncio
async def test_create_is_author_setting_and_replay_does_not_duplicate_it(auth_client, neo_env):
    r = neo_env
    request_headers = {'Idempotency-Key': str(uuid4())}
    body = r.identity | {'rel_type': '同事', 'note': '保持职务距离'}
    first = await auth_client.post(r.url, headers=request_headers, json=body)
    assert first.status_code == 202, first.text
    second = await auth_client.post(r.url, headers=request_headers, json=body)
    assert second.json() == first.json()
    snapshot = await original(auth_client, r, rel_type='同事')
    assert len(snapshot['entries']) == 1 and snapshot['entries'][0]['properties']['origin'] == 'author'
    assert snapshot['fields']['note'] == '保持职务距离' and len(r.graph.edges) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_id', 'no_version', 'bad_id', 'bad_version', 'empty_patch', 'extra_field', 'blank_type'])
async def test_bad_writes_are_rejected_without_changing_graph_ownership(auth_client, neo_env, case):
    r = neo_env
    request_headers = await headers(auth_client, r)
    body = r.identity | {'label': '修改'}
    if case == 'no_id': request_headers.pop('Idempotency-Key')
    elif case == 'no_version': request_headers.pop('If-Match')
    elif case == 'bad_id': request_headers['Idempotency-Key'] = 'not-a-uuid'
    elif case == 'bad_version': request_headers['If-Match'] = 'old'
    elif case == 'empty_patch': body = r.identity
    elif case == 'extra_field': body['origin'] = 'author'
    elif case == 'blank_type': body['new_rel_type'] = '  '
    response = await auth_client.put(r.url, headers=request_headers, json=body)
    assert response.status_code == 422, response.text
    assert r.graph.mutations == 0 and current(r)['label'] == '旧标签'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['stale', 'reused_id'])
async def test_conflicts_return_stable_codes_and_keep_the_newer_value(auth_client, neo_env, case):
    r = neo_env
    request_headers = await headers(auth_client, r)
    assert (await auth_client.put(r.url, headers=request_headers, json=r.identity | {'label': '先提交'})).status_code == 202
    if case == 'stale': request_headers['Idempotency-Key'] = str(uuid4())
    response = await auth_client.put(r.url, headers=request_headers, json=r.identity | {'label': '迟到'} )
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == ('source_changed' if case == 'stale' else 'request_id_reused')
    assert current(r)['label'] == '先提交' and r.graph.mutations == 1


@pytest.mark.asyncio
async def test_sql_failure_never_returns_saved_and_same_request_can_confirm(auth_client, neo_env):
    r = neo_env
    request_headers = await headers(auth_client, r)
    r.fail_commit = True
    body = r.identity | {'note': '尚待同步的作者要求'}
    response = await auth_client.put(r.url, headers=request_headers, json=body)
    assert response.status_code == 503 and response.json()['detail']['code'] == 'sync_pending'
    assert 'private' not in response.text
    retry = await auth_client.put(r.url, headers=request_headers, json=body)
    assert retry.status_code == 202 and retry.json()['status'] == 'saved'
    assert current(r)['note'] == body['note'] and r.graph.mutations == 1


@pytest.mark.asyncio
async def test_missing_project_is_rejected_before_reading_or_writing_graph(auth_client, neo_env):
    r = neo_env
    url = r.url.replace(str(r.pid), str(uuid4()))
    response = await auth_client.get(url + '/record', params=r.identity)
    assert response.status_code == 404, response.text
    assert not r.graph.calls
    response = await auth_client.post(url, headers={'Idempotency-Key': str(uuid4())}, json=r.identity)
    assert response.status_code == 404 and not r.graph.calls


@pytest.mark.asyncio
async def test_relationship_routes_keep_authentication(client, neo_env):
    r = neo_env
    response = await client.get(r.url + '/record', params=r.identity)
    assert response.status_code == 401 and not r.graph.calls


def test_unexpected_conflict_details_are_not_returned_to_clients():
    from app.api.relationship_writes import public_error
    from app.services.relationship_write_contract import RelationshipWriteConflict
    error = public_error(RelationshipWriteConflict('private_database_detail'))
    assert error.status_code == 409 and error.detail['code'] == 'relationship_conflict'
    assert 'private_database_detail' not in str(error.detail)
