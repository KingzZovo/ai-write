"""The real arc worker must see book-level inputs, not just a prompt promise."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.project import Outline, Project, WorldRule
from app.services.outline_authority import record_text_edit
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count


def book_material(r):
    WorldRule.__table__.create(r.engine, checkfirst=True)
    oid, rule_id = uuid4(), uuid4()
    premise = '姐弟依靠缝纫和赶集的实际进项还债；本书不新增工厂订单。' * 180
    rule = '渡口封航时必须等水位回落，铜铃不能恢复、不能重复施术。' * 200
    text = '开篇说明来处。' * 300 + '\n【书纲中段】借据还在姐姐的柜底，本弧不能提前烧掉。\n' + '远期结局保持原安排。' * 300
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == r.pid).values(premise=premise, genre='现实题材'))
        db.execute(sa.insert(Outline).values(id=oid, project_id=r.pid, level='book', version=2,
            is_confirmed=1, content_json={'raw_text': text, 'main_plot': '原来结构中的未来安排'}))
        db.execute(sa.insert(WorldRule).values(id=rule_id, project_id=r.pid, category='世界限制', rule_text=rule))
    r.graph.rules.append({'id': str(rule_id), 'category': '世界限制', 'text': rule, 'origin': 'author'})
    return NS(oid=oid, rule_id=rule_id, premise=premise, rule=rule, text=text)


@pytest.mark.asyncio
async def test_complete_book_premise_rules_and_middle_outline_reach_every_actual_phase(planning_book):
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=11)
    await submit(r, body); await run(r, body['request_id'])
    assert snapshot(r, body['request_id'])['status'] == 'completed'
    assert len(r.calls) == 4
    for _, user, _ in r.calls:
        assert data.premise in user
        assert data.rule in user
        assert data.text in user
        assert '设定与计划不能证明事件已经发生' in user


@pytest.mark.asyncio
async def test_current_author_book_text_wins_over_old_structure_and_newer_unconfirmed_version(planning_book):
    r = planning_book; data = book_material(r)
    revised = '作者当前书纲：姐姐没有工作单位，借据仍保留，不能把规划的还清当成事实。' * 300
    with Session(r.engine) as db:
        row = db.get(Outline, data.oid)
        row.content_json = record_text_edit(row.content_json, {**row.content_json, 'raw_text': revised})
        db.add(Outline(project_id=r.pid, level='book', version=99, is_confirmed=0,
            content_json={'raw_text': '未确认候选：临时获得整批订单'}))
        db.add(Outline(project_id=r.foreign, level='book', version=1000, is_confirmed=1,
            content_json={'raw_text': '另一部书的世界设定'}))
        db.commit()
    body = await request_body(r, target=4)
    await submit(r, body); await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed'
    for _, user, _ in r.calls:
        assert revised in user
        assert '原来结构中的未来安排' not in user
        assert '临时获得整批订单' not in user and '另一部书的世界设定' not in user
    with Session(r.engine) as db:
        assert db.get(Outline, data.oid).content_json['main_plot'] == '原来结构中的未来安排'


@pytest.mark.asyncio
async def test_confirming_another_book_candidate_invalidates_the_preview_without_replacing_either_plan(planning_book):
    from app.api.outlines import confirm_outline
    r = planning_book; data = book_material(r)
    candidate_id = uuid4()
    candidate = {'raw_text': '作者新方案：冬天之后才揭开旧借据的来历。'}
    with r.engine.begin() as db:
        db.execute(sa.insert(Outline).values(id=candidate_id, project_id=r.pid, level='book', version=3,
            is_confirmed=0, content_json=candidate))
    body = await request_body(r, target=4)
    async with r.sessions() as db:
        await confirm_outline(str(r.pid), str(candidate_id), None, db)
        await db.commit()
    response = await submit(r, body)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'arc_planning_source_changed'
    assert snapshot(r, body['request_id']) is None and not r.calls and not r.published
    with Session(r.engine) as db:
        assert db.get(Outline, candidate_id).content_json == candidate
        assert db.get(Outline, data.oid).content_json['raw_text'] == data.text


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['premise', 'outline', 'rule'])
async def test_book_material_change_after_source_read_rejects_submission_without_calling_model(planning_book, field):
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=4)
    with r.engine.begin() as db:
        if field == 'premise': db.execute(sa.update(Project).where(Project.id == r.pid).values(premise='后来改过的立意'))
        elif field == 'outline': db.execute(sa.update(Outline).where(Outline.id == data.oid).values(content_json={'raw_text': '后来改过的书纲'}))
        else:
            db.execute(sa.update(WorldRule).where(WorldRule.id == data.rule_id).values(rule_text='后来改过的规则'))
            r.graph.rules[0]['text'] = '后来改过的规则'
    response = await submit(r, body)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'arc_planning_source_changed'
    assert snapshot(r, body['request_id']) is None and not r.calls and not r.published


@pytest.mark.asyncio
async def test_changing_a_rule_during_a_model_call_preserves_the_original_source_and_no_new_volume(planning_book):
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=4)
    await submit(r, body)
    before = snapshot(r, body['request_id'])
    async def edit_rule(*args):
        r.graph.rules[0]['text'] = '作者刚修改了世界限制'
    r.before_call = edit_rule
    await run(r, body['request_id'])
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and after['source_json'] == before['source_json']
    assert len(r.calls) == 1 and volume_count(r) == 1
    with Session(r.engine) as db:
        assert db.get(WorldRule, data.rule_id).rule_text == data.rule  # Stale PG cannot validate the old request.


@pytest.mark.asyncio
async def test_repair_and_restart_preserve_the_same_complete_book_inputs(planning_book):
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=11)
    await submit(r, body)
    async def interrupt(task, user, *args):
        if 'start=6, end=10' in user: raise asyncio.CancelledError()
    r.before_call = interrupt
    with pytest.raises(asyncio.CancelledError): await run(r, body['request_id'])
    saved = snapshot(r, body['request_id'])
    r.before_call = None
    await run(r, body['request_id'], manual=True)
    final = snapshot(r, body['request_id'])
    assert final['status'] == 'completed'
    assert final['journal_json']['stages'][:2] == saved['journal_json']['stages'][:2]
    for _, user, _ in r.calls:
        assert data.text in user and data.rule in user and data.premise in user


@pytest.mark.asyncio
async def test_source_and_durable_details_expose_the_actual_full_book_material(planning_book):
    r = planning_book; data = book_material(r)
    async with r.client() as client:
        source = (await client.get(path(r, '/source'))).json()
    assert data.text in source['book_material_text'] and data.rule in source['book_material_text']
    body = await request_body(r, target=4)
    await submit(r, body)
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['book_material_text'] == source['book_material_text']
    assert detail['integrity_valid'] and not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['empty_revision', 'failed_outline', 'unavailable_revision', 'bad_payload', 'raw_type', 'empty_rule'])
async def test_damaged_current_book_material_is_not_replaced_by_older_or_empty_inputs(planning_book, broken):
    r = planning_book; data = book_material(r)
    with Session(r.engine) as db:
        row = db.get(Outline, data.oid)
        if broken == 'empty_revision': row.content_json = record_text_edit(row.content_json, {**row.content_json, 'raw_text': ''})
        elif broken == 'failed_outline': row.content_json = {'parse_error': True, 'raw_text': '不完整书纲'}
        elif broken == 'unavailable_revision':
            row.content_json = {'_order_unavailable': True, '_author_text_revision': 'saved', 'raw_text': '保留但不能继续使用的当前书纲。'}
        elif broken == 'bad_payload': row.content_json = ['错误类型']
        elif broken == 'raw_type': row.content_json = {'raw_text': [], 'main_plot': '不能把坏文字转为旧结构继续'}
        else:
            db.get(WorldRule, data.rule_id).rule_text = ''
            r.graph.rules[0]['text'] = ''
        db.commit()
    async with r.client() as client:
        source = await client.get(path(r, '/source'))
    assert source.status_code == 409 and source.json()['detail']['code'] == 'arc_planning_book_material_invalid'
    assert not r.calls and not r.published


@pytest.mark.asyncio
async def test_temporary_book_material_read_failure_can_resume_the_same_request(planning_book, monkeypatch):
    from app.services import arc_planning_sources as sources
    r = planning_book; book_material(r)
    body = await request_body(r, target=4)
    await submit(r, body)
    before = snapshot(r, body['request_id'])
    original = sources.read_planning_book_source
    async def unavailable(*args, **kwargs): raise sa.exc.SQLAlchemyError('temporary private database detail')
    monkeypatch.setattr(sources, 'read_planning_book_source', unavailable)
    async with r.client() as client:
        response = await client.get(path(r, '/source'))
    assert response.status_code == 503 and 'private' not in response.text
    await run(r, body['request_id'])
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['can_resume'] and detail['status'] == 'needs_review'
    assert not r.calls and detail['source'] == before['source_json']
    monkeypatch.setattr(sources, 'read_planning_book_source', original)
    await run(r, body['request_id'], manual=True)
    final = snapshot(r, body['request_id'])
    assert final['status'] == 'completed' and final['execution_attempts'] == 1
    assert final['request_json'] == before['request_json']


@pytest.mark.asyncio
async def test_old_request_remains_readable_without_fabricating_new_book_provenance(planning_book):
    from app.services.arc_planning_contract import author_material, previous_material, source_token
    from app.services.arc_planning_journal import new_journal
    from app.services.entity_sources import digest
    r = planning_book; book_material(r)
    body = await request_body(r, target=4)
    await submit(r, body)
    # Reconstruct the exact prior protocol: no book snapshot and no book input.
    with Session(r.engine) as db:
        row = db.get(Request, UUID(body['request_id']))
        source = deepcopy(row.source_json); source['version'] = 1; source.pop('book_context'); source.pop('character_context'); source.pop('cognition_context')
        intent = deepcopy(row.request_json); intent['expected_source'] = source_token(r.pid, source)
        journal = new_journal(author_material(intent), previous_material(source))
        row.source_json, row.source_hash = source, digest(source)
        row.request_json, row.request_hash = intent, digest(intent)
        row.journal_json, row.journal_hash = journal, digest(journal)
        db.commit()
    before = snapshot(r, body['request_id'])
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['integrity_valid'] and detail['book_material_text'] is None
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and not r.calls and volume_count(r) == 1
    assert after['source_json'] == before['source_json'] and after['journal_json'] == before['journal_json']
    assert after['execution_attempts'] == 0


@pytest.mark.asyncio
async def test_unverified_synopsis_and_unused_configuration_do_not_become_book_facts(planning_book):
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=4)
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == r.pid).values(settings_json={
            'book_synopsis': '无来源旧摘要说债已还清', 'unused_configuration': '配置字段不能当成故事事实'}))
    assert (await submit(r, body)).status_code == 202
    await run(r, body['request_id'])
    assert snapshot(r, body['request_id'])['status'] == 'completed'
    for _, user, _ in r.calls:
        assert data.premise in user
        assert '无来源旧摘要说债已还清' not in user and '配置字段不能当成故事事实' not in user


@pytest.mark.asyncio
async def test_a_rule_without_a_category_keeps_its_complete_actual_text(planning_book):
    r = planning_book; data = book_material(r)
    with r.engine.begin() as db:
        db.execute(sa.update(WorldRule).where(WorldRule.id == data.rule_id).values(category=''))
    r.graph.rules[0]['category'] = ''
    body = await request_body(r, target=4)
    await submit(r, body); await run(r, body['request_id'])
    assert snapshot(r, body['request_id'])['status'] == 'completed'
    assert all(data.rule in user for _, user, _ in r.calls)


@pytest.mark.asyncio
async def test_graph_outage_preserves_accepted_stages_and_resumes_with_original_budget(planning_book):
    r = planning_book; book_material(r)
    body = await request_body(r, target=11)
    await submit(r, body)
    async def interrupt(task, user, *args):
        if 'start=6, end=10' in user: raise asyncio.CancelledError()
    r.before_call = interrupt
    with pytest.raises(asyncio.CancelledError):
        await run(r, body['request_id'])
    before = snapshot(r, body['request_id'])
    r.before_call = None
    r.graph.failure = RuntimeError('private graph endpoint failure')
    async with r.client() as client:
        response = await client.get(path(r, '/source'))
    assert response.status_code == 503 and 'private' not in response.text
    await run(r, body['request_id'], manual=True)
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['can_resume'] and detail['status'] == 'needs_review'
    failed = snapshot(r, body['request_id'])
    assert failed['source_json'] == before['source_json'] and failed['journal_json'] == before['journal_json']
    assert failed['execution_attempts'] == 1 and len(r.calls) == 3
    r.graph.failure = None
    await run(r, body['request_id'], manual=True)
    final = snapshot(r, body['request_id'])
    assert final['status'] == 'completed' and final['execution_attempts'] == 2
    assert final['journal_json']['stages'][:2] == before['journal_json']['stages'][:2]
    assert len(final['journal_json']['stages'][2]['attempts']) == 2
    assert final['request_json'] == before['request_json'] and final['source_json'] == before['source_json']


@pytest.mark.asyncio
async def test_legacy_pg_book_snapshot_stays_readable_without_fabricated_graph_provenance(planning_book):
    from app.services.arc_planning_contract import author_material, previous_material, book_material as render_book, source_token
    from app.services.arc_planning_journal import new_journal
    from app.services.entity_sources import digest
    r = planning_book; data = book_material(r)
    body = await request_body(r, target=4)
    await submit(r, body)
    with Session(r.engine) as db:
        row = db.get(Request, UUID(body['request_id']))
        source = deepcopy(row.source_json); source['version'] = 2; source.pop('character_context'); source.pop('cognition_context')
        book = source['book_context']; book['version'] = 1
        book['world_rules'] = [{'id': str(data.rule_id), 'category': '世界限制', 'rule_text': data.rule}]
        intent = deepcopy(row.request_json); intent['expected_source'] = source_token(r.pid, source)
        original_text = render_book(source)
        journal = new_journal(author_material(intent), previous_material(source), original_text)
        row.source_json, row.source_hash = source, digest(source)
        row.request_json, row.request_hash = intent, digest(intent)
        row.journal_json, row.journal_hash = journal, digest(journal)
        db.commit()
    before = snapshot(r, body['request_id'])
    async with r.client() as client:
        detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['integrity_valid'] and detail['book_material_text'] == original_text
    assert data.rule in original_text and data.text in original_text
    assert '｜作者设定' not in original_text and 'book_source' not in detail['source']['book_context']
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and not r.calls and after['execution_attempts'] == 0
    assert after['source_json'] == before['source_json'] and after['journal_json'] == before['journal_json']
    assert volume_count(r) == 1
