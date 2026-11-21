"""Source-bound author decisions on SQLite; no model or broker IO."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, update

from app.services.character_cognition import validate_changes, serialize_for_prompt
from app.services import character_cognition as history
from app.services import cognition_reader as reader
from app.services import cognition_edits as edits
from app.services import cognition_extraction as extraction
from app.services import cognition_backfill as backfill
from app.services import cognition_requests as requests
from app.services.entity_sources import resolve_entity_source, digest
from app.services.context_errors import RequiredContextUnavailable
from app.models.cognition import ChapterCognition as Record, CognitionRevision as Revision, CognitionRequest as Receipt
from app.models.project import Chapter
from tests.test_cognition_workflow_offline import cognition_db, state_db, run, stored, aged_record, add_state
from tests.test_cognition_author_workflow import schedule, backfill_row, retry


async def view(r, chapter_id=None):
    return await reader.read_chapter_cognition(r.db, r.pid, chapter_id or r.source.chapter_id, include_source=True)


async def decide(r, changes=None, *, mode='author', snapshot=None, request_id=None, actor='编辑甲', chapter_id=None):
    chapter_id = chapter_id or r.source.chapter_id
    snapshot = snapshot or await view(r, chapter_id)
    async with r.sessions() as db:
        result = await edits.save_cognition_decision(db, r.pid, chapter_id, request_id or uuid4(),
            snapshot['source_hash'], snapshot['version'], mode=mode, changes=changes, actor=actor)
        await db.commit()
        return result


async def belief_source(r):
    prose = '沈青以为父亲已经读了信，却又怀疑回信是陈川代写的。'
    r.session.execute(update(Chapter).values(content_text=prose)); r.session.commit()
    r.source = await resolve_entity_source(r.db, r.pid, 0, r.source.chapter_id)
    # Deliberately wrong automatic interpretation, to be corrected by author.
    r.changes = [dict(character='沈青', learns='父亲已经读信', evidence=prose)]
    r.model.return_value = NS(text=json.dumps(r.changes, ensure_ascii=False), finish_reason='stop')
    return [dict(character='沈青', believes='以为父亲已经读信', evidence=prose),
            dict(character='沈青', suspects='怀疑回信是陈川代写', evidence=prose)]


@pytest.mark.parametrize('kind,fact', [
    ('believes', '以为父亲已经读信'),
    ('suspects', '怀疑回信由陈川代写'),
])
def test_explicit_belief_or_suspicion_has_its_own_kind(kind, fact):
    prose = '沈青以为父亲已经读了信，却又怀疑回信是陈川代写的。'
    changes = [{'character': '沈青', kind: fact, 'evidence': prose}]
    assert validate_changes(changes, prose) == changes


@pytest.mark.parametrize('kind,label', [('believes', '当时确信'), ('suspects', '当时怀疑')])
def test_subjective_knowledge_is_not_rendered_as_established_fact(kind, label):
    rendered = serialize_for_prompt({'沈青': {'history': [{
        'chapter_idx': 1, 'kind': kind, 'fact': '信已送到', 'evidence': '沈青认定信已送到。',
    }]}}, max_chars=1200)
    assert label in rendered


@pytest.mark.parametrize('kind', ['still_unknown', 'believes', 'suspects'])
def test_reader_has_no_invented_subjective_state(kind):
    with pytest.raises(ValueError):
        validate_changes([dict(character='__reader__', evidence='原文', **{kind: '事实'})], '原文')


@pytest.mark.asyncio
async def test_author_interpretation_overrides_model_without_destroying_it(cognition_db):
    r = cognition_db
    corrected = await belief_source(r)
    await run(r)
    before = await view(r)
    accepted = await decide(r, corrected)
    row, detail = stored(r), await view(r)
    assert detail['source_content'] == r.source.content
    assert detail['source_mode'] == 'author' and detail['changes'] == corrected
    assert detail['model_changes'] == r.changes and row.changes_json == r.changes
    assert detail['version'] != before['version'] and not detail['can_retry']
    assert str(row.author_revision_id) == accepted['revision_id']
    revision = r.session.get(Revision, row.author_revision_id)
    assert revision.actor == '编辑甲' and revision.model_snapshot_json['changes'] == r.changes
    assert revision.base_version == before['version'] and revision.previous_id is None
    ledger = await history.load_ledger(r.db, r.pid, before_global_idx=901)
    events = ledger['沈青']['history']
    assert [e['kind'] for e in events] == ['believes', 'suspects']
    prompt = serialize_for_prompt(ledger, max_chars=2000)
    assert '当时确信' in prompt and '当时怀疑' in prompt and '作者校订' in prompt
    assert r.source.content in prompt
    revisions = await reader.list_revisions(r.db, r.pid, r.source.chapter_id)
    assert revisions['revisions'][0]['is_current'] and revisions['revisions'][0]['matches_current_source']


@pytest.mark.asyncio
async def test_author_empty_correction_stops_all_automatic_paths(cognition_db):
    r = cognition_db
    await aged_record(r, 'pending')
    old_cycle = str(stored(r).cycle_id)
    accepted = await decide(r, [])
    assert (await view(r))['changes'] == [] and (await view(r))['status'] == 'completed'
    assert await history.load_ledger(r.db, r.pid, before_global_idx=901) == {}
    assert (await run(r, cycle_id=old_cycle))['status'] == 'superseded'
    assert (await run(r, cycle_id=accepted['cycle_id']))['status'] == 'superseded'
    async with r.sessions() as db:
        await extraction.enqueue_cognition(db, r.source)
        await db.commit()
    assert str(stored(r).cycle_id) == accepted['cycle_id']
    job = await schedule(r)
    assert await backfill.advance_backfills(r.sessions) == 1
    assert backfill_row(r, job['backfill_id']).existing_count == 1
    assert (await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *a: pytest.fail('author work dispatched')))['dispatched'] == 0
    with pytest.raises(requests.CognitionConflict, match='作者校订'):
        await retry(r)
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_request_replays_original_receipt_after_later_edit(cognition_db):
    r = cognition_db
    initial, request_id = await view(r), uuid4()
    first = await decide(r, [], snapshot=initial, request_id=request_id)
    second = await decide(r, r.changes)
    assert await decide(r, [], snapshot=initial, request_id=request_id) == first
    assert (await view(r))['revision_id'] == second['revision_id']
    with pytest.raises(requests.CognitionConflict):
        await decide(r, r.changes, snapshot=initial, request_id=request_id)
    with pytest.raises(requests.CognitionConflict):
        await decide(r, [], snapshot=initial, request_id=request_id, actor='编辑乙')
    revisions = r.session.scalars(select(Revision)).all()
    assert len(revisions) == 2
    assert r.session.get(Revision, UUID(second['revision_id'])).previous_id == UUID(first['revision_id'])


@pytest.mark.asyncio
async def test_second_author_cannot_overwrite_a_new_decision_with_old_baseline(cognition_db):
    r = cognition_db
    baseline = await view(r)
    await decide(r, [])
    with pytest.raises(requests.CognitionConflict, match='认知记录已变化'):
        await decide(r, r.changes, snapshot=baseline)
    assert (await view(r))['changes'] == []
    assert len(r.session.scalars(select(Revision)).all()) == len(r.session.scalars(select(Receipt)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['body', 'title', 'order', 'status', 'delete'])
async def test_edit_rechecks_entire_current_source_before_writing(cognition_db, mutation):
    r = cognition_db
    baseline = await view(r)
    if mutation == 'delete': r.session.execute(delete(Chapter))
    else:
        field, value = {'body': ('content_text', '新保存稿。'), 'title': ('title', '新章名'),
            'order': ('global_idx', 901), 'status': ('status', 'draft')}[mutation]
        r.session.execute(update(Chapter).values(**{field: value}))
    r.session.commit()
    with pytest.raises(requests.CognitionConflict): await decide(r, [], snapshot=baseline)
    assert r.session.scalar(select(Revision)) is None and r.session.scalar(select(Receipt)) is None
    r.model.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [None, {}, [{'character': '陈川', 'learns': '得知信', 'evidence': '不存在的句子'}],
    [{'character': '陈川', 'learns': '得知信', 'believes': '以为信', 'evidence': '陈川不知道信已送到。'}]])
async def test_invalid_author_payload_has_no_partial_request_or_revision(cognition_db, changes):
    r = cognition_db
    with pytest.raises(edits.CognitionEditInvalid): await decide(r, changes)
    assert r.session.scalar(select(Revision)) is None and r.session.scalar(select(Receipt)) is None
    assert stored(r) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['valid', 'malformed', 'exception'])
async def test_edit_during_model_call_fences_late_write_and_format_repair(cognition_db, response):
    r = cognition_db
    async def model(**kwargs):
        await decide(r, [])
        if response == 'exception': raise RuntimeError('model interrupted')
        return NS(text='invalid' if response == 'malformed' else json.dumps(r.changes), finish_reason='stop')
    r.model.side_effect = model
    if response == 'exception':
        with pytest.raises(RuntimeError): await run(r)
    else:
        assert (await run(r))['status'] == 'superseded'
    detail = await view(r)
    assert detail['changes'] == [] and detail['source_mode'] == 'author' and detail['status'] == 'completed'
    assert stored(r).attempt_token is None and stored(r).changes_json is None
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_reuses_verified_automatic_empty_result(cognition_db):
    r = cognition_db
    r.model.return_value = NS(text='[]', finish_reason='stop')
    await run(r)
    author = await decide(r, r.changes)
    restored = await decide(r, mode='auto')
    assert restored['outcome'] == 'already_complete' and restored['source_mode'] == 'auto'
    assert (await view(r))['changes'] == [] and (await view(r))['source_mode'] == 'auto'
    assert (await run(r, cycle_id=author['cycle_id']))['status'] == 'superseded'
    assert (await run(r, cycle_id=restored['cycle_id']))['reused']
    assert await history.load_ledger(r.db, r.pid, before_global_idx=901) == {}
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_without_valid_model_cache_schedules_new_cycle_once(cognition_db):
    r = cognition_db
    author = await decide(r, [])
    snapshot, request_id = await view(r), uuid4()
    restored = await decide(r, mode='auto', snapshot=snapshot, request_id=request_id)
    assert restored['outcome'] == 'scheduled' and (await view(r))['status'] == 'pending'
    assert (await run(r, cycle_id=author['cycle_id']))['status'] == 'superseded'
    assert (await run(r, cycle_id=restored['cycle_id']))['status'] == 'completed'
    assert await decide(r, mode='auto', snapshot=snapshot, request_id=request_id) == restored
    assert (await view(r))['changes'] == r.changes
    r.model.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing_pointer', 'missing_revision', 'wrong_record', 'wrong_project',
                                       'wrong_source', 'wrong_hash', 'wrong_quote', 'wrong_mode'])
async def test_broken_author_decision_never_falls_back_to_model(cognition_db, corruption):
    r = cognition_db
    await run(r)
    await decide(r, [])
    row = stored(r)
    revision = r.session.get(Revision, row.author_revision_id)
    if corruption == 'missing_pointer': row.author_revision_id = None
    elif corruption == 'missing_revision': r.session.delete(revision)
    elif corruption == 'wrong_hash': revision.payload_hash = '0' * 64
    else:
        if corruption == 'wrong_record': revision.record_id = uuid4()
        elif corruption == 'wrong_project': revision.project_id = uuid4()
        elif corruption == 'wrong_source': revision.source_hash = '0' * 64
        elif corruption == 'wrong_quote': revision.changes_json = [dict(character='陈川', learns='得知信', evidence='缺失的原句')]
        elif corruption == 'wrong_mode': revision.mode = 'auto'; revision.changes_json = None
        revision.payload_hash = digest(reader.revision_payload(revision))
    r.session.commit()
    detail = await view(r)
    assert detail['status'] == 'invalid' and detail['changes'] is None and detail['model_changes'] == r.changes
    assert not detail['can_retry']
    with pytest.raises(RequiredContextUnavailable): await history.load_ledger(r.db, r.pid, before_global_idx=901)
    with pytest.raises(requests.CognitionConflict): await retry(r)
    # Explicit author correction can repair a broken pointer without accepting model output.
    await decide(r, [])
    assert (await view(r))['changes'] == []


@pytest.mark.asyncio
async def test_broken_restore_decision_cannot_pay_for_a_model_call(cognition_db):
    r = cognition_db
    await decide(r, [])
    restored = await decide(r, mode='auto')
    r.session.execute(delete(Revision).where(Revision.id == UUID(restored['revision_id']))); r.session.commit()
    assert (await view(r))['status'] == 'invalid' and not (await view(r))['can_retry']
    assert '明确恢复自动提取' in (await view(r))['message']
    assert (await run(r, cycle_id=restored['cycle_id']))['reason'] == 'author_decision_invalid'
    with pytest.raises(requests.CognitionConflict): await retry(r)
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_source_correction_mid_read_is_not_a_mixed_ledger(cognition_db, monkeypatch):
    r = cognition_db
    await run(r)
    real = reader.load_source_records
    calls = 0
    async def changed(db, project_id, sources, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: await decide(r, [])
        return await real(db, project_id, sources, **kwargs)
    monkeypatch.setattr(reader, 'load_source_records', changed)
    with pytest.raises(RequiredContextUnavailable, match='decision_changed'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
async def test_changed_body_does_not_carry_author_correction_to_new_source(cognition_db):
    r = cognition_db
    await decide(r, [])
    r.session.execute(update(Chapter).values(content_text='陈川已经知道真相。')); r.session.commit()
    assert (await view(r))['status'] == 'missing' and (await view(r))['source_mode'] == 'auto'
    old = await reader.list_revisions(r.db, r.pid, r.source.chapter_id)
    assert len(old['revisions']) == 1 and not old['revisions'][0]['matches_current_source']
    assert not old['revisions'][0]['is_current']


@pytest.mark.asyncio
async def test_revision_and_fixed_receipt_remain_after_chapter_deletion(cognition_db):
    r = cognition_db
    initial, request_id = await view(r), uuid4()
    accepted = await decide(r, [], snapshot=initial, request_id=request_id)
    r.session.execute(delete(Record)); r.session.execute(delete(Chapter)); r.session.commit()
    assert await decide(r, [], snapshot=initial, request_id=request_id) == accepted
    audit = await reader.list_revisions(r.db, r.pid, r.source.chapter_id)
    assert len(audit['revisions']) == 1 and audit['revisions'][0]['changes'] == []
    assert not audit['revisions'][0]['matches_current_source']
    with pytest.raises(reader.CognitionNotFound): await reader.list_revisions(r.db, uuid4(), r.source.chapter_id)
    assert not any(fk.column.table.name in {'chapters', 'chapter_cognition_extractions'} for fk in Revision.__table__.foreign_keys)


@pytest.mark.asyncio
async def test_twenty_author_rows_are_read_in_one_revision_query(cognition_db):
    r = cognition_db
    await decide(r, [])
    for idx in range(901, 920):
        source = await add_state(r, idx, {})
        await decide(r, [], chapter_id=source.chapter_id)
    r.queries.clear()
    listed = await reader.list_chapter_cognition(r.db, r.pid)
    assert len(listed['chapters']) == 20 and all(c['source_mode'] == 'author' for c in listed['chapters'])
    assert len(r.queries) <= 7
    assert all(c['changes_count'] == 0 and c['status'] == 'completed' for c in listed['chapters'])


@pytest.mark.asyncio
async def test_api_validates_actor_and_replays_after_commit_acknowledgment_loss(cognition_db):
    from app.api import cognition as api
    r = cognition_db
    app = FastAPI()
    app.include_router(api.router)
    @app.middleware('http')
    async def authenticated_principal(request, call_next):
        request.state.auth_username = '已登录作者'
        return await call_next(request)
    fail_ack = True
    async def commit():
        nonlocal fail_ack
        r.session.commit()
        if fail_ack:
            fail_ack = False
            raise RuntimeError('simulated lost acknowledgment')
    async def rollback(): r.session.rollback()
    async def db_dependency():
        async with r.sessions() as db:
            yield NS(execute=db.execute, scalar=db.scalar, get=db.get, commit=commit, rollback=rollback)
    app.dependency_overrides[api.get_db] = db_dependency
    baseline = await view(r)
    body = dict(request_id=str(uuid4()), source_hash=baseline['source_hash'], version=baseline['version'], changes=[])
    prefix = f'/api/projects/{r.pid}/cognition/chapters/{r.source.chapter_id}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post(prefix+'/edit', json=dict(body, actor='伪造作者'))).status_code == 422
        failed = await client.post(prefix+'/edit', json=body)
        assert failed.status_code == 503 and 'simulated' not in failed.text
        received = await client.post(prefix+'/edit', json=body)
        assert received.status_code == 200 and received.json()['outcome'] == 'saved'
        detail = (await client.get(prefix+'?include_source=true')).json()
        assert detail['source_content'] == r.source.content and detail['changes'] == []
        audit = (await client.get(prefix+'/revisions')).json()['revisions']
        assert len(audit) == 1 and audit[0]['actor'] == '已登录作者'
        restored = await client.post(prefix+'/restore-auto', json=dict(request_id=str(uuid4()),
            source_hash=detail['source_hash'], version=detail['version']))
        assert restored.status_code == 200 and restored.json()['outcome'] == 'scheduled'
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_editorial_correction_keeps_original_two_chapter_information_gap(cognition_db):
    """Replay recorded CPA output; editorial correction is explicit, not model grading."""
    r = cognition_db
    cases = json.loads((Path(__file__).parents[2] / 'docs/evaluation/cognition-history-20261007/samples.json').read_text())['cases']
    first, second = cases['uncertainty'], cases['discovery']
    r.session.execute(update(Chapter).values(content_text=first['source'])); r.session.commit()
    r.source = await resolve_entity_source(r.db, r.pid, 0, r.source.chapter_id)
    r.model.return_value = NS(text=json.dumps(first['runtime_candidate_changes']), finish_reason='stop')
    await run(r)
    corrected = deepcopy(first['runtime_candidate_changes']) + [dict(character='沈青',
        suspects='怀疑陈川把车坏当作借口，但还没有证据。',
        evidence='沈青望着车站门口亮着灯的车，觉得他在找借口，却没有证据。')]
    await decide(r, corrected)
    next_source = await add_state(r, 901, {}, body=second['source'])
    r.model.return_value = NS(text=json.dumps(second['runtime_candidate_changes']), finish_reason='stop')
    await extraction.run_cognition_extraction(next_source, sessions=r.sessions, engine=None)
    ledger = await history.load_ledger(r.db, r.pid, before_global_idx=902)
    events = ledger['沈青']['history']
    assert [(item['chapter_idx'], item['kind'], item['source_mode']) for item in events] == [
        (900, 'still_unknown', 'author'), (900, 'suspects', 'author'),
        (901, 'learns', 'auto'), (901, 'still_unknown', 'auto')]
    rendered = serialize_for_prompt(ledger, max_chars=3000, priority_names=['沈青'])
    assert all(change['evidence'] in rendered for change in corrected + second['runtime_candidate_changes'])
    assert '当时怀疑' in rendered and '作者校订' in rendered and '[CH-901]' in rendered
    # No automatic fact merge, no attribution of who put the notice away.
    assert len(events) == 4 and '父亲把通知压' not in rendered
