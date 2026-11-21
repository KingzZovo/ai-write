"""Current character material must reach real arc calls and retained requests."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.project import Chapter, Character
from app.services.entity_sources import resolve_entity_source
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count
from tests.support.character_graph import CharacterGraph
from tests.support.planning_cognition import cognition_values
from app.models.cognition import ChapterCognition


@pytest.fixture
def character_book(planning_book, monkeypatch):
    r = planning_book
    r.character_graph = CharacterGraph(r.pid)
    async def driver(): yield r.character_graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    return r


async def character_material(r):
    body = '林禾的左手仍使不上力。陈川答应替她送信，但没有答应替她偿还欠款。信封留在陈川的内袋。他走进回程船舱。'
    with r.engine.begin() as db:
        db.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(content_text=body, status='completed'))
    async with r.sessions() as db:
        source = await resolve_entity_source(db, r.pid, 1, r.cid)
    # This fixture focuses on state/relationships. Explicitly record no
    # cognition changes rather than leaving required extraction unfinished.
    with r.engine.begin() as db:
        db.execute(sa.insert(ChapterCognition).values(**cognition_values(source, [])))
    provenance = dict(source_chapter_id=source.chapter_id, source_hash=source.source_hash, global_idx=source.global_idx)
    profile = {'性格与顾虑': '林禾先核实钱款再答应别人，不靠临时增加的收入脱困。' * 180,
               'aliases': ['阿禾'], 'location': '旧档案里的渡口位置，不能盖过正文状态。'}
    r.character_graph.profiles.append(dict(node_id='graph-person-1', id=str(uuid4()), name='林禾',
        profile_json=json.dumps(profile, ensure_ascii=False), profile_origin='author_edit'))
    r.character_graph.states.append(dict(name='林禾', status=json.dumps({
        '左手': '暂不能握紧绳索', '已经还清': False, '铜铃': None, '欠款': 1000,
        '待办': ['核对借据', '和船主商量']}, ensure_ascii=False), **provenance))
    r.character_graph.relationships.append(dict(name='陈川', target='林禾', rel_type='受托送信，未承诺代偿',
        raw_type='受托送信，未承诺代偿', event_kind='new', evidence=body, **provenance))
    r.character_graph.locations.append(dict(name='陈川', location='回程船舱', chapter_end=None, **provenance))
    return NS(body=body, profile=profile, source=source)


@pytest.mark.asyncio
async def test_graph_reader_keeps_current_profile_and_each_body_source(character_book):
    from app.services.entity_context import verified_world_snapshot
    r = character_book; await character_material(r)
    async with r.sessions() as db:
        material = await verified_world_snapshot(db, r.character_graph, r.pid, 2, include_profiles=True)
    assert material.profiles[0]['name'] == '林禾'
    assert material.characters[0].field_sources['左手']['source_chapter_id'] == str(r.cid)
    assert material.location_records[0]['location'] == '回程船舱'


@pytest.mark.asyncio
async def test_current_profiles_states_directional_relationships_and_locations_reach_all_phases(character_book):
    r = character_book; data = await character_material(r)
    body = await request_body(r, target=11)
    await submit(r, body); await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed' and len(r.calls) == 4
    for _, user, _ in r.calls:
        assert data.profile['性格与顾虑'] in user
        assert '暂不能握紧绳索' in user and '"已经还清": false' in user and '"铜铃": null' in user
        assert '陈川 → 林禾' in user and '受托送信，未承诺代偿' in user and data.body in user
        assert '回程船舱' in user
        assert '档案不证明正文已经发生' in user
    assert state['source_json']['version'] == 5
    material = state['source_json']['character_context']
    assert material['before_global_idx'] == 2 and material['target_volume_idx'] == 6
    assert material['states'][0]['field_sources']['左手']['source_chapter_id'] == str(r.cid)


@pytest.mark.asyncio
async def test_current_graph_profile_wins_over_stale_pg_and_empty_author_profile_stays_empty(character_book):
    r = character_book; await character_material(r)
    Character.__table__.create(r.engine)
    with r.engine.begin() as db:
        db.execute(sa.insert(Character).values(project_id=r.pid, name='林禾', profile_json={'旧人设': '凭新订单突然发家'}))
    r.character_graph.profiles[0]['profile_json'] = '{}'
    body = await request_body(r, target=4)
    await submit(r, body); await run(r, body['request_id'])
    assert snapshot(r, body['request_id'])['status'] == 'completed'
    assert all('凭新订单突然发家' not in user for _, user, _ in r.calls)
    material = snapshot(r, body['request_id'])['source_json']['character_context']
    assert material['profiles'][0]['profile_json'] == '{}'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['profile', 'body', 'location', 'relationship', 'delete_profile', 'node_identity'])
async def test_character_source_change_after_preview_rejects_submission_without_a_model_call(character_book, change):
    r = character_book; await character_material(r)
    body = await request_body(r, target=4)
    if change == 'profile': r.character_graph.profiles[0]['profile_json'] = '{"性格":"作者刚改的顾虑"}'
    elif change == 'location': r.character_graph.locations[0]['location'] = '堤岸'
    elif change == 'relationship': r.character_graph.relationships[0]['raw_type'] = '另一份完整关系措辞'
    elif change == 'delete_profile': r.character_graph.profiles.clear()
    elif change == 'node_identity': r.character_graph.profiles[0]['node_id'] = 'recreated-graph-node'
    else:
        with r.engine.begin() as db:
            db.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(content_text='作者的新稿：信封没有交出去。'))
    response = await submit(r, body)
    assert response.status_code == (503 if change == 'body' else 409)
    assert response.json()['detail']['code'] == ('arc_planning_cognition_material_unavailable' if change == 'body' else 'arc_planning_source_changed')
    assert snapshot(r, body['request_id']) is None and not r.calls


@pytest.mark.asyncio
async def test_a_source_change_during_the_model_call_keeps_original_material_and_stops_save(character_book):
    r = character_book; await character_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    before = snapshot(r, body['request_id'])
    async def edit(*args): r.character_graph.profiles[0]['profile_json'] = '{"顾虑":"作者新改的人物"}'
    r.before_call = edit
    await run(r, body['request_id'])
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and after['error_code'] == 'arc_planning_source_changed'
    assert after['source_json'] == before['source_json']
    assert len(r.calls) == 1 and volume_count(r) == 1


@pytest.mark.asyncio
async def test_graph_unavailable_is_not_an_empty_cast_and_can_resume_without_losing_accepted_stages(character_book):
    r = character_book; await character_material(r)
    body = await request_body(r, target=11); await submit(r, body)
    async def fail_after_meta(task, *args):
        if task == 'arc_outline_batch': r.character_graph.failure = RuntimeError('private graph error')
    r.before_call = fail_after_meta
    await run(r, body['request_id'])
    failed = snapshot(r, body['request_id'])
    assert failed['status'] == 'needs_review'
    assert failed['error_code'] == 'arc_planning_character_material_unavailable'
    assert 'private' not in failed['error_message']
    r.character_graph.failure = None; r.before_call = None
    await run(r, body['request_id'], manual=True)
    final = snapshot(r, body['request_id'])
    assert final['status'] == 'completed'
    assert final['journal_json']['stages'][0] == failed['journal_json']['stages'][0]
    assert final['execution_attempts'] == failed['execution_attempts'] + 1
    old_attempt = failed['journal_json']['stages'][1]['attempts'][0]
    attempts = final['journal_json']['stages'][1]['attempts']
    assert len(attempts) == 2 and attempts[0]['input'] == old_attempt['input']
    assert '暂不能握紧绳索' in attempts[1]['input']


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', ['[]', '{', 'null', '{"risk":NaN}'])
async def test_bad_current_graph_profile_is_not_replaced_by_empty_or_pg_material(character_book, profile):
    r = character_book; await character_material(r)
    r.character_graph.profiles[0]['profile_json'] = profile
    async with r.client() as client:
        response = await client.get(path(r, '/source'))
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'arc_planning_character_material_invalid'
    assert not r.calls


@pytest.mark.asyncio
async def test_durable_details_show_the_same_complete_character_material_as_actual_calls(character_book):
    r = character_book; data = await character_material(r)
    async with r.client() as client:
        source = (await client.get(path(r, '/source'))).json()
    assert data.profile['性格与顾虑'] in source['book_material_text']
    body = await request_body(r, target=4); await submit(r, body)
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['book_material_text'] == source['book_material_text']
    assert detail['source']['character_context']['before_global_idx'] == 2


@pytest.mark.asyncio
async def test_repair_input_retains_complete_character_material_and_original_failed_output(character_book):
    r = character_book; data = await character_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    broken = '错误输出：不能把未还清写成已经还清。' * 160
    async def malformed_once(task, *args):
        if len(r.calls) == 1: return NS(text=broken, finish_reason='stop')
    r.before_call = malformed_once
    await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed' and len(r.calls) == 3
    assert len(state['journal_json']['stages'][0]['attempts']) == 2
    assert broken in r.calls[1][1]
    assert all(data.profile['性格与顾虑'] in user and '暂不能握紧绳索' in user for _, user, _ in r.calls)


@pytest.mark.asyncio
async def test_previous_version_three_request_keeps_its_original_inputs_without_inventing_a_cast(character_book):
    from app.models.arc_planning import ArcPlanningRequest as Request
    from app.services.arc_planning_contract import author_material, previous_material, book_material, source_token
    from app.services.arc_planning_journal import new_journal
    from app.services.entity_sources import digest
    r = character_book; await character_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    with Session(r.engine) as db:
        row = db.get(Request, UUID(body['request_id']))
        source = deepcopy(row.source_json); source['version'] = 3; source.pop('character_context'); source.pop('cognition_context')
        intent = deepcopy(row.request_json); intent['expected_source'] = source_token(r.pid, source)
        material = book_material(source)
        journal = new_journal(author_material(intent), previous_material(source), material)
        row.source_json, row.source_hash = source, digest(source)
        row.request_json, row.request_hash = intent, digest(intent)
        row.journal_json, row.journal_hash = journal, digest(journal)
        db.commit()
    before = snapshot(r, body['request_id'])
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['integrity_valid'] and detail['book_material_text'] == material
    assert 'character_context' not in detail['source'] and '暂不能握紧绳索' not in material
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['source_json'] == before['source_json'] and after['journal_json'] == before['journal_json']
    assert after['status'] == 'needs_review' and after['execution_attempts'] == 0 and not r.calls
