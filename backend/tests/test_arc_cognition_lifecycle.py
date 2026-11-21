"""Real planning reservations, repairs and recovery around cognition selection."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
from sqlalchemy import update

from app.models.arc_planning import ArcPlanningRequest as Request
from app.services import arc_planning_execution as execution
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count
from tests.test_arc_cognition_material import cognition_book, cognition_material, author_decision


def observe_choices(r, monkeypatch, hook=None):
    original = execution.prepare_planning_cognition
    choices = []
    async def prepare(source, query, *, db, **kwargs):
        assert not db.in_transaction() and not db.bind.in_transaction()
        choices.append(query)
        if hook is not None: await hook(source, query, db)
        return await original(source, query, db=db, **kwargs)
    monkeypatch.setattr(execution, 'prepare_planning_cognition', prepare)
    return choices


@pytest.mark.asyncio
async def test_each_repair_uses_its_full_failed_draft_for_cognition_recall(cognition_book, monkeypatch):
    r = cognition_book; await cognition_material(r)
    choices = observe_choices(r, monkeypatch)
    body = await request_body(r, target=11); await submit(r, body)
    failed = '失败试稿提到船夫与旧井的消息，这些尚不是正文事实。' * 300
    async def malformed(task, *args):
        if len(r.calls) == 1: return NS(text=failed, finish_reason='stop')
    r.before_call = malformed
    await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed' and len(choices) == len(r.calls) == 5
    assert failed not in choices[0] and failed in choices[1]
    assert all(body['background'] in query for query in choices)
    assert '此前完整章节规划' in choices[-1] and '修好以后' in choices[-1]
    attempts = state['journal_json']['stages'][0]['attempts']
    assert attempts[0]['cognition']['query_hash'] != attempts[1]['cognition']['query_hash']
    assert [a['input'] for s in state['journal_json']['stages'] for a in s['attempts']] == [call[1] for call in r.calls]


@pytest.mark.asyncio
async def test_later_interrupted_batch_keeps_original_choices_and_retrieves_only_the_recovery_attempt(cognition_book, monkeypatch):
    r = cognition_book; await cognition_material(r)
    queries = observe_choices(r, monkeypatch)
    body = await request_body(r, target=11); await submit(r, body)
    async def stop(task, user, *args):
        if 'start=6, end=10' in user: raise asyncio.CancelledError()
    r.before_call = stop
    with pytest.raises(asyncio.CancelledError): await run(r, body['request_id'])
    before = snapshot(r, body['request_id'])
    assert before['accepted_chapters'] == 5 and len(queries) == 3
    r.before_call = None
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'completed' and after['execution_attempts'] == 2
    assert after['source_json'] == before['source_json']
    assert after['journal_json']['stages'][:2] == before['journal_json']['stages'][:2]
    old = before['journal_json']['stages'][2]['attempts'][0]
    attempts = after['journal_json']['stages'][2]['attempts']
    assert attempts[0]['cognition'] == old['cognition'] and attempts[0]['input'] == old['input']
    assert attempts[0]['state'] == 'interrupted' and attempts[1]['number'] == 2
    assert '上次调用中断' in queries[3] and len(queries) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'author_edit', 'connection_lost'])
async def test_changes_during_selection_prevent_a_late_model_call(cognition_book, monkeypatch, action):
    r = cognition_book; data = await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    async def interfere(source, query, db):
        if action == 'cancel':
            async with r.client() as client:
                assert (await client.post(path(r, f'/{body["request_id"]}/cancel'))).status_code == 200
        elif action == 'author_edit': await author_decision(r, data, [])
        else: db.guard.revoke()
    observe_choices(r, monkeypatch, interfere)
    await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert not r.calls and not state['journal_json']['stages'] and volume_count(r) == 1
    if action == 'cancel': assert state['status'] == 'cancelled'
    elif action == 'author_edit': assert state['error_code'] == 'arc_planning_source_changed'
    else: assert state['status'] == 'running'  # Keep the last confirmed execution, not a guessed failure.


@pytest.mark.asyncio
async def test_unavailable_selection_can_resume_with_accepted_stages_and_without_an_extra_model_attempt(cognition_book, monkeypatch):
    r = cognition_book; await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    failed = False
    async def fail_after_meta(source, query, db):
        nonlocal failed
        if r.calls and not failed:
            failed = True
            raise RequiredContextUnavailable('private upstream description')
    observe_choices(r, monkeypatch, fail_after_meta)
    await run(r, body['request_id'])
    before = snapshot(r, body['request_id'])
    assert before['error_code'] == 'arc_planning_cognition_retrieval_unavailable' and len(r.calls) == 1
    assert len(before['journal_json']['stages']) == 1 and 'private' not in before['error_message']
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'completed' and len(r.calls) == 2
    assert after['journal_json']['stages'][0] == before['journal_json']['stages'][0]
    assert len(after['journal_json']['stages'][1]['attempts']) == 1


@pytest.mark.asyncio
async def test_retrieval_failures_cannot_buy_more_than_the_original_three_executions(cognition_book, monkeypatch):
    r = cognition_book; await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    async def fail(*args): raise RequiredContextUnavailable('character_cognition_retrieval_unavailable')
    queries = observe_choices(r, monkeypatch, fail)
    for _ in range(4): await run(r, body['request_id'], manual=True)
    state = snapshot(r, body['request_id'])
    assert state['execution_attempts'] == len(queries) == 3 and not r.calls
    assert state['error_code'] == 'execution_budget_exhausted'
    assert not state['journal_json']['stages']


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['text', 'rank', 'query', 'reference', 'missing'])
async def test_saved_selection_corruption_cannot_be_repaired_by_recomputing_only_the_journal_digest(cognition_book, monkeypatch, damage):
    r = cognition_book; await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    async def stop(*args): raise asyncio.CancelledError()
    r.before_call = stop
    with pytest.raises(asyncio.CancelledError): await run(r, body['request_id'])
    before = snapshot(r, body['request_id'])
    journal = deepcopy(before['journal_json']); attempt = journal['stages'][0]['attempts'][0]
    if damage == 'text': attempt['cognition']['text'] = '这不是任何已核对的认知原句。'
    elif damage == 'rank':
        attempt['cognition'].update(space_id='a' * 64, ranked_keys=['b' * 64])
    elif damage == 'query': attempt['cognition']['query_hash'] = 'c' * 64
    elif damage == 'reference': attempt['cognition']['reference_hash'] = 'd' * 64
    else: attempt.pop('cognition')
    with r.engine.begin() as db:
        db.execute(update(Request).where(Request.id == UUID(body['request_id'])).values(journal_json=journal, journal_hash=digest(journal)))
    r.before_call = None
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and after['error_code'] == 'arc_planning_record_invalid'
    assert after['journal_json'] == journal and after['execution_attempts'] == before['execution_attempts']
    assert len(r.calls) == 1 and volume_count(r) == 1
