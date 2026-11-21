"""Persistent call attempts must survive interruption before model output."""
import asyncio
from copy import deepcopy
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.generation_run import CriticReport, GenerationRun
from app.models.project import Chapter, ChapterVariant
from app.services import generation_runner as runner
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.review_contract import incomplete_critic_report, make_critic_report
from tests.test_generation_execution import DRAFT, LocalSession, book, counts, resume, start, state

HARD = {'severity': 'hard', 'category': 'resource_continuity',
        'desc': '必须核对交付金额。', 'location': '两千元', 'source': 'llm'}


@pytest.mark.asyncio
async def test_rewrite_budget_is_reserved_before_the_model_and_never_refunded_on_resume(book, monkeypatch):
    r = book
    identity = uuid4()
    r.review.side_effect = None
    r.review.return_value = make_critic_report(DRAFT, issues=[HARD], stages=[{'name': 'critic', 'status': 'complete'}])
    rewrite = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)
    with pytest.raises(asyncio.CancelledError):
        await start(r, request_id=str(identity), max_rewrite_count=1)
    checkpoint = state(r, identity)
    assert checkpoint['rewrite_count'] == 1
    assert checkpoint['checkpoint_data']['drafting']['text'] == DRAFT
    rewrite.side_effect = None
    rewrite.return_value = {'text': DRAFT + '新版。', 'delivery_status': 'complete'}
    response = await resume(r, identity)
    assert response.status_code == 200 and response.json()['status'] == 'needs_review'
    assert response.json()['rewrite_count'] == 1
    rewrite.assert_awaited_once()
    assert state(r, identity)['checkpoint_data']['drafting']['text'] == DRAFT


@pytest.mark.asyncio
async def test_three_unconfirmed_draft_calls_exhaust_the_original_phase_budget(book):
    r = book
    identity = uuid4()
    r.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(r, request_id=str(identity))
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError): await resume(r, identity)
    response = await resume(r, identity)
    assert response.status_code == 200 and response.json()['status'] == 'needs_review'
    assert response.json()['stop_reason'] == 'model_call_budget_exhausted'
    assert r.drafting.await_count == 3
    assert len(response.json()['checkpoint_data']['attempt_journal']['attempts']) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('journal', [None, {'version': 999}, {'version': 1, 'attempts': 'broken'}])
async def test_missing_or_damaged_call_history_cannot_be_invented_during_resume(book, journal):
    r = book
    identity = uuid4()
    r.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(r, request_id=str(identity))
    with Session(r.engine) as db:
        row = db.get(GenerationRun, identity)
        cp = dict(row.checkpoint_data)
        if journal is None: cp.pop('attempt_journal', None)
        else: cp['attempt_journal'] = journal
        row.checkpoint_data = cp
        db.commit()
    r.drafting.side_effect = None
    response = await resume(r, identity)
    assert response.status_code == 200 and response.json()['status'] == 'needs_review'
    assert response.json()['stop_reason'] == 'model_call_history_unverified'
    assert response.json()['checkpoint_data'].get('attempt_journal') == journal
    assert r.drafting.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('id', 'not-a-uuid'), ('kind', []), ('kind', 'embedding'), ('slot', True), ('slot', -1),
    ('number', 0), ('number', True), ('status', []), ('status', 'complete'),
    ('started_at', '2026-10-09T00:00:00'), ('started_at', 'broken'),
    ('input_sha256', 'bad'), ('before_delivery_sha256', 'bad'),
])
async def test_corrupt_attempt_fields_stop_without_normalizing_or_calling_again(book, field, value):
    identity = uuid4()
    book.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        run = db.get(GenerationRun, identity)
        cp = deepcopy(run.checkpoint_data)
        cp['attempt_journal']['attempts'][-1][field] = value
        damaged = deepcopy(cp['attempt_journal'])
        run.checkpoint_data = cp
        flag_modified(run, 'checkpoint_data')  # JSON true and integer 1 compare equal in Python.
        db.commit()
    response = await resume(book, identity)
    assert response.status_code == 200
    assert response.json()['stop_reason'] == 'model_call_history_unverified'
    assert response.json()['checkpoint_data']['attempt_journal'] == damaged
    assert book.drafting.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['removed_attempt', 'duplicated_uuid', 'foreign_run', 'larger_limit', 'bad_count', 'changed_output'])
async def test_damaged_history_or_checkpoint_cannot_reset_the_phase_budget(book, damage):
    identity = uuid4()
    book.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        run = db.get(GenerationRun, identity)
        cp = deepcopy(run.checkpoint_data)
        journal = cp['attempt_journal']
        if damage == 'removed_attempt': journal['attempts'] = []
        elif damage == 'duplicated_uuid':
            first = journal['attempts'][0]
            first.update(status='interrupted', finished_at=first['started_at'], reason='unconfirmed_on_resume')
            journal['attempts'].append({**first, 'number': 2})
            journal['attempt_count'] = 2
        elif damage == 'foreign_run': journal['run_id'] = str(uuid4())
        elif damage == 'larger_limit': journal['phase_limit'] = 4
        elif damage == 'bad_count': journal['attempt_count'] = True
        elif damage == 'changed_output': cp['drafting'] = {'text': '已另存的候选，不能丢失。'}
        original = deepcopy(cp)
        run.checkpoint_data = cp
        flag_modified(run, 'checkpoint_data')
        db.commit()
    response = await resume(book, identity)
    assert response.status_code == 200 and response.json()['status'] == 'needs_review'
    assert response.json()['stop_reason'] == 'model_call_history_unverified'
    assert response.json()['checkpoint_data']['attempt_journal'] == original['attempt_journal']
    assert response.json()['checkpoint_data'].get('drafting') == original.get('drafting')
    assert book.drafting.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_enabled,initial_n,new_enabled,new_n', [
    ('true', '2', 'false', '8'), ('false', '1', 'true', '8'), ('true', '8', 'true', '1'),
])
async def test_resume_keeps_the_original_candidate_configuration(book, monkeypatch, initial_enabled, initial_n, new_enabled, new_n):
    monkeypatch.setenv('BVSR_ENABLED', initial_enabled)
    monkeypatch.setenv('BVSR_N', initial_n)
    identity = uuid4()
    book.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    monkeypatch.setenv('BVSR_ENABLED', new_enabled)
    monkeypatch.setenv('BVSR_N', new_n)
    book.drafting.side_effect = None
    result = (await resume(book, identity)).json()
    assert result['status'] == 'done'
    journal = result['checkpoint_data']['attempt_journal']
    assert journal['variant_count'] == int(initial_n)
    assert journal['bvsr_enabled'] is (initial_enabled == 'true')
    assert book.drafting.await_count == int(initial_n) + 1
    assert len({entry['id'] for entry in journal['attempts']}) == len(journal['attempts'])
    assert journal['attempts'][0]['status'] == 'interrupted'
    assert journal['attempts'][1]['number'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['critic', 'variant_draft', 'variant_critic'])
async def test_each_interrupted_phase_has_a_finite_budget_and_keeps_delivered_manuscripts(book, monkeypatch, phase):
    if phase != 'critic':
        monkeypatch.setenv('BVSR_ENABLED', 'true')
        monkeypatch.setenv('BVSR_N', '2')
    identity = uuid4()
    interrupted = 0
    async def draft(_run, _db, _plan):
        nonlocal interrupted
        if phase == 'variant_draft' and _run.checkpoint_data['attempt_journal']['attempts'][-1]['kind'] == phase:
            interrupted += 1
            raise asyncio.CancelledError()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    async def review(**kwargs):
        nonlocal interrupted
        with Session(book.engine) as reader:
            record = reader.get(GenerationRun, identity).checkpoint_data['attempt_journal']['attempts'][-1]
        if record['kind'] == phase and (phase == 'critic' or record['slot'] == 1):
            interrupted += 1
            raise asyncio.CancelledError()
        return make_critic_report(kwargs['draft'], issues=[], stages=[{'name': 'critic', 'status': 'complete'}])
    book.drafting.side_effect = draft
    book.review.side_effect = review
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError): await resume(book, identity)
    before = state(book, identity)['checkpoint_data']['drafting']
    response = await resume(book, identity)
    assert response.status_code == 200 and response.json()['stop_reason'] == 'model_call_budget_exhausted'
    assert interrupted == 3
    assert response.json()['checkpoint_data']['drafting'] == before
    assert before['text'] == DRAFT
    assert book.drafting.await_count == (4 if phase == 'variant_draft' else 2 if phase == 'variant_critic' else 1)


@pytest.mark.asyncio
async def test_incomplete_returned_reviews_also_consume_the_original_review_budget(book):
    book.review.side_effect = None
    book.review.return_value = incomplete_critic_report(DRAFT)
    response = await start(book)
    identity = response.json()['id']
    for _ in range(2):
        assert (await resume(book, identity)).json()['stop_reason'] == 'review_incomplete'
    result = (await resume(book, identity)).json()
    assert result['stop_reason'] == 'model_call_budget_exhausted'
    assert book.review.await_count == 3 and book.drafting.await_count == 1
    assert counts(book) == (1, 3, 0)
    for _ in range(2):
        assert (await resume(book, identity)).json() == result
    assert book.review.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('known_output', ['variant', 'report', 'checkpoint'])
async def test_unknown_persisted_delivery_stops_instead_of_overwriting_it(book, known_output):
    identity = uuid4()
    book.review.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        run = db.get(GenerationRun, identity)
        if known_output == 'report':
            db.add(CriticReport(id=uuid4(), run_id=identity, round=1, issues_json=[], review_json={}))
        elif known_output == 'checkpoint':
            runner._checkpoint_set(run, 'drafting', {'text': '中断期间另一份已保存候选。'})
        else:
            # A separate draft attempt, interrupted after the first review,
            # cannot overlook a target candidate stored outside its receipt.
            journal = deepcopy(run.checkpoint_data['attempt_journal'])
            journal['attempts'][-1]['kind'] = 'variant_draft'
            journal['attempts'][-1]['slot'] = 1
            journal.update(bvsr_enabled=True, variant_count=2)
            cp = deepcopy(run.checkpoint_data)
            cp['attempt_journal'] = journal
            run.checkpoint_data = cp
            db.add(ChapterVariant(id=uuid4(), chapter_id=book.cid, run_id=identity,
                variant_idx=1, content_text='已保存候选必须保留。', word_count=10))
        db.commit()
    before = state(book, identity)
    query_log = []
    def query(_conn, _cursor, statement, *_rest): query_log.append(statement)
    sa.event.listen(book.engine, 'before_cursor_execute', query)
    try:
        response = await resume(book, identity)
    finally:
        sa.event.remove(book.engine, 'before_cursor_execute', query)
    assert response.status_code == 200
    assert response.json()['stop_reason'] == 'model_call_delivery_unverified'
    assert response.json()['checkpoint_data']['drafting'] == before['checkpoint_data']['drafting']
    assert response.json()['checkpoint_data']['attempt_journal'] == before['checkpoint_data']['attempt_journal']
    assert book.review.await_count == 1 and book.drafting.await_count == 1
    assert not any('chapter_variants.content_text' in q for q in query_log)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['draft', 'rewrite'])
@pytest.mark.parametrize('committed', [False, True])
async def test_model_waits_for_acknowledged_reservation_commit(book, monkeypatch, phase, committed):
    identity = uuid4()
    if phase == 'rewrite':
        book.review.side_effect = None
        book.review.return_value = make_critic_report(DRAFT, issues=[HARD], stages=[{'name': 'critic', 'status': 'complete'}])
    rewrite = AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'})
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)
    original = LocalSession.commit
    failed = False
    async def lose_ack(db):
        nonlocal failed
        for row in db.session.dirty:
            if isinstance(row, GenerationRun):
                records = (row.checkpoint_data.get('attempt_journal') or {}).get('attempts') or []
                if not failed and records and records[-1]['kind'] == phase and records[-1]['status'] == 'started':
                    failed = True
                    if committed: await original(db)
                    raise GenerationExecutionUnavailable()
        await original(db)
    monkeypatch.setattr(LocalSession, 'commit', lose_ack)
    response = await start(book, request_id=str(identity), max_rewrite_count=1)
    assert response.status_code == 503 and failed
    assert response.json()['detail']['run_id'] == str(identity)
    assert book.drafting.await_count == (1 if phase == 'rewrite' else 0)
    rewrite.assert_not_awaited()
    saved = state(book, identity)
    records = saved['checkpoint_data']['attempt_journal']['attempts']
    assert sum(e['kind'] == phase for e in records) == int(committed)
    if phase == 'rewrite':
        assert saved['rewrite_count'] == int(committed)
        assert saved['checkpoint_data']['drafting']['text'] == DRAFT


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True])
async def test_report_checkpoint_and_delivery_proof_are_one_transaction(book, monkeypatch, committed):
    identity = uuid4()
    original = LocalSession.commit
    failed = False
    async def lose_ack(db):
        nonlocal failed
        for row in db.session.dirty:
            if isinstance(row, GenerationRun):
                records = (row.checkpoint_data.get('attempt_journal') or {}).get('attempts') or []
                if not failed and records and records[-1]['kind'] == 'critic' and records[-1]['status'] == 'returned':
                    failed = True
                    assert row.checkpoint_data['critic']['report']['draft_sha256']
                    assert any(isinstance(x, CriticReport) and str(x.id) == records[-1]['id'] for x in db.session.new)
                    if committed: await original(db)
                    raise GenerationExecutionUnavailable()
        await original(db)
    monkeypatch.setattr(LocalSession, 'commit', lose_ack)
    response = await start(book, request_id=str(identity))
    assert response.status_code == 503 and failed
    saved = state(book, identity)['checkpoint_data']
    assert ('critic' in saved) is committed
    assert counts(book) == (1, int(committed), 0)
    assert saved['attempt_journal']['attempts'][-1]['status'] == ('returned' if committed else 'started')
    assert saved['drafting']['text'] == DRAFT


@pytest.mark.asyncio
async def test_consumed_rewrite_attempt_and_original_draft_survive_author_source_change(book, monkeypatch):
    identity = uuid4()
    book.review.side_effect = None
    book.review.return_value = make_critic_report(DRAFT, issues=[HARD], stages=[{'name': 'critic', 'status': 'complete'}])
    rewrite = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)
    with pytest.raises(asyncio.CancelledError):
        await start(book, request_id=str(identity), max_rewrite_count=1)
    before = state(book, identity)
    with Session(book.engine) as db:
        db.get(Chapter, book.cid).content_text = '作者已经自行修改的保存稿。'
        db.commit()
    response = await resume(book, identity)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_run_source_changed'
    assert state(book, identity) == before and before['rewrite_count'] == 1
    rewrite.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_of_one_chapter_does_not_load_a_five_million_character_book(book):
    identity = uuid4()
    body = '本章明确发生的事情与人物动作。' * 350
    with Session(book.engine) as db:
        volume_id = db.get(Chapter, book.cid).volume_id
        db.add_all([Chapter(id=uuid4(), volume_id=volume_id, chapter_idx=i, global_idx=i,
            title=f'长书第{i}章', content_text=body) for i in range(2, 1006)])
        db.commit()
        total = db.scalar(sa.select(sa.func.sum(sa.func.length(Chapter.content_text))))
    assert total > 5_000_000
    book.drafting.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    body_queries = []
    def observe(_connection, _cursor, statement, parameters, *_rest):
        if statement.lstrip().upper().startswith('SELECT') and 'chapters.content_text' in statement:
            body_queries.append((statement, parameters))
    book.drafting.side_effect = None
    sa.event.listen(book.engine, 'before_cursor_execute', observe)
    try:
        response = await resume(book, identity)
    finally:
        sa.event.remove(book.engine, 'before_cursor_execute', observe)
    assert response.status_code == 200 and response.json()['status'] == 'done'
    assert len(body_queries) == 1
    assert 'WHERE chapters.id =' in body_queries[0][0]
    assert book.cid.hex in body_queries[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('drafting', ['broken']), ('drafting', {'text': 17}),
                                        ('critic', ['broken']), ('draft_history', 'broken')])
async def test_damaged_nested_checkpoint_remains_readable_when_recovery_stops(book, field, value):
    identity = uuid4()
    book.review.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        run = db.get(GenerationRun, identity)
        cp = deepcopy(run.checkpoint_data)
        cp[field] = value
        run.checkpoint_data = cp
        db.commit()
    response = await resume(book, identity)
    assert response.status_code == 200
    assert response.json()['status'] == 'needs_review'
    assert response.json()['stop_reason'] == 'model_call_history_unverified'
    assert response.json()['checkpoint_data'][field] == value
    async with book.client() as client:
        observed = await client.get(f'/api/generation-runs/{identity}')
    assert observed.status_code == 200
    assert observed.json()['checkpoint_data'][field] == value
    assert book.review.await_count == 1 and book.drafting.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [None, [], ['retained fragment'], 'retained fragment', 17])
async def test_unparsed_whole_checkpoint_is_retained_in_the_review_response(book, payload):
    identity = uuid4()
    book.review.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        db.get(GenerationRun, identity).checkpoint_data = payload
        db.commit()
    async with book.client() as client:
        original = await client.get(f'/api/generation-runs/{identity}')
    assert original.status_code == 200
    assert original.json()['checkpoint_data'] == {'unparsed_checkpoint': payload}
    result = (await resume(book, identity)).json()
    assert result['status'] == 'needs_review'
    assert result['stop_reason'] == 'model_call_history_unverified'
    assert result['checkpoint_data']['unparsed_checkpoint'] == payload
    assert book.review.await_count == 1 and book.drafting.await_count == 1


@pytest.mark.asyncio
async def test_interrupted_rewrite_uses_only_the_remaining_original_budget(book, monkeypatch):
    identity = uuid4()
    revised = DRAFT + '他接过钱，收进了书包。'
    async def review(**kwargs):
        return make_critic_report(kwargs['draft'], issues=[HARD] if kwargs['draft'] == DRAFT else [],
                                  stages=[{'name': 'critic', 'status': 'complete'}])
    book.review.side_effect = review
    rewrite = AsyncMock(side_effect=[asyncio.CancelledError(), {'text': revised, 'delivery_status': 'complete'}])
    monkeypatch.setattr(runner, '_phase_rewrite', rewrite)
    with pytest.raises(asyncio.CancelledError):
        await start(book, request_id=str(identity), max_rewrite_count=2)
    response = await resume(book, identity)
    assert response.status_code == 200 and response.json()['status'] == 'done'
    assert response.json()['rewrite_count'] == 2
    cp = response.json()['checkpoint_data']
    assert cp['drafting']['text'] == revised
    assert cp['draft_history'] == [{'text': DRAFT, 'rewrite_count': 1}]
    entries = [e for e in cp['attempt_journal']['attempts'] if e['kind'] == 'rewrite']
    assert [(e['slot'], e['status']) for e in entries] == [(0, 'interrupted'), (1, 'returned')]
    assert rewrite.await_count == 2 and book.drafting.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['draft', 'variant_draft'])
async def test_unconfirmed_draft_cannot_ignore_a_report_saved_without_its_call_receipt(book, monkeypatch, kind):
    identity = uuid4()
    if kind == 'variant_draft':
        monkeypatch.setenv('BVSR_ENABLED', 'true')
        monkeypatch.setenv('BVSR_N', '2')
    async def draft(run, _db, _planning):
        if run.checkpoint_data['attempt_journal']['attempts'][-1]['kind'] == kind:
            raise asyncio.CancelledError()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    book.drafting.side_effect = draft
    with pytest.raises(asyncio.CancelledError): await start(book, request_id=str(identity))
    with Session(book.engine) as db:
        db.add(CriticReport(id=uuid4(), run_id=identity, round=1, issues_json=[], review_json={}))
        db.commit()
    calls = book.drafting.await_count, book.review.await_count
    result = await resume(book, identity)
    assert result.status_code == 200
    assert result.json()['stop_reason'] == 'model_call_delivery_unverified'
    assert (book.drafting.await_count, book.review.await_count) == calls
