"""Legacy HTTP run requests: retries observe the original, never execute it.

Uses the public SQLite/ASGI fixture and real persisted checkpoints. Model
delivery and PostgreSQL's execution lock are substituted, not the runner.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.generation_run import GenerationRun, GenerationRunSubmission
from app.models.project import Chapter, Project, Volume
from app.services import generation_execution as ownership, generation_runner as runner
from app.services.generation_run_submissions import (
    RunSubmissionError, run_submission_intent,
)
from tests.test_generation_execution import (
    DRAFT, LocalSession, book, counts, resume, saved_run, state,
)


ORIGINAL = '作者保存的原稿。'


def request(r, **values):
    return {'request_id': str(uuid4()), 'project_id': str(r.pid),
            'chapter_id': str(r.cid), 'max_rewrite_count': 0,
            'expected_content_text': ORIGINAL, **values}


async def post(r, body):
    async with r.client() as client:
        return await client.post('/api/generation-runs/start', json=body)


async def get(r, identity):
    async with r.client() as client:
        return await client.get(f'/api/generation-runs/{identity}')


def receipt_state(r, identity):
    with Session(r.engine) as session:
        receipt = session.get(GenerationRunSubmission, UUID(str(identity)))
        return {column.name: deepcopy(getattr(receipt, column.name)) for column in receipt.__table__.columns}


@pytest.mark.asyncio
@pytest.mark.parametrize('field,code', [
    ('request_id', 'generation_request_id_required'),
    ('expected_content_text', 'chapter_content_precondition_required'),
])
async def test_missing_submission_precondition_never_creates_or_calls_model(book, field, code):
    body = request(book)
    del body[field]
    response = await post(book, body)
    assert response.status_code == 428, response.text
    assert response.json()['detail']['code'] == code
    assert counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_request_returns_original_saved_checkpoint_without_another_execution(book):
    body = request(book)
    first = await post(book, body)
    assert first.status_code == 200, first.text
    assert first.json()['id'] == body['request_id']
    before = state(book, UUID(first.json()['id']))
    locks = list(book.lock_entries)
    second = await post(book, body)
    assert second.status_code == 200 and second.json() == first.json()
    assert book.lock_entries == locks
    assert state(book, UUID(body['request_id'])) == before
    assert counts(book) == (1, 1, 0)
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['manuscript', 'budget', 'chapter', 'project'])
async def test_reusing_request_for_other_input_is_a_conflict_not_a_new_run(book, change):
    body = request(book)
    response = await post(book, body)
    assert response.status_code == 200
    altered = dict(body)
    if change == 'manuscript': altered['expected_content_text'] += '\n新增末句'
    if change == 'budget': altered['max_rewrite_count'] = 1
    if change == 'chapter': altered['chapter_id'] = str(uuid4())
    if change == 'project': altered['project_id'] = str(book.other)
    second = await post(book, altered)
    assert second.status_code == 409, second.text
    assert second.json()['detail']['code'] == 'generation_request_conflict'
    assert counts(book) == (1, 1, 0)
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['', ORIGINAL + ' ', ORIGINAL + '\n', '作者保存的新稿。'])
async def test_start_compares_the_full_manuscript_without_trimming(book, expected):
    response = await post(book, request(book, expected_content_text=expected))
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'chapter_edit_conflict'
    assert counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_author_edit_and_trash_still_reads_original_result(book):
    body = request(book)
    first = await post(book, body)
    assert first.status_code == 200
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text='作者的新稿。'))
        connection.execute(sa.update(Project).where(Project.id == book.pid).values(deleted_at=datetime.now(timezone.utc)))
    book.held.add(book.pid)
    retry = await post(book, body)
    assert retry.status_code == 200 and retry.json() == first.json()
    assert counts(book) == (1, 1, 0)
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_legacy_source_is_readable_but_cannot_resume_automatically(book):
    rid = saved_run(book, with_submission=False, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    before = state(book, rid)
    result = await resume(book, rid)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'generation_run_source_unverified'
    assert state(book, rid) == before
    read = await get(book, rid)
    assert read.status_code == 200 and read.json()['checkpoint_data']['drafting']['text'] == DRAFT
    book.planning.assert_not_awaited()
    book.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('request_id', 'bad-uuid'), ('request_id', None),
    ('max_rewrite_count', True), ('max_rewrite_count', '2'), ('max_rewrite_count', -1),
    ('max_rewrite_count', 9), ('expected_content_text', 123),
    ('expected_content_text', None), ('unexpected_setting', True),
])
async def test_invalid_contract_stops_before_any_database_lock(book, field, value):
    response = await post(book, request(book, **{field: value}))
    assert response.status_code == (428 if value is None else 422)
    assert counts(book) == (0, 0, 0) and book.lock_entries == []
    book.planning.assert_not_awaited()


@pytest.mark.parametrize('field,value', [
    ('request_id', None), ('request_id', 'bad-uuid'), ('project_id', 'bad-uuid'),
    ('chapter_id', 'bad-uuid'), ('expected_content_text', None),
    ('expected_content_text', False), ('max_rewrite_count', 1.5), ('max_rewrite_count', True),
    ('expected_content_text', '\ud800'),
])
def test_service_validates_non_http_callers(field, value):
    values = dict(request_id=uuid4(), project_id=uuid4(), chapter_id=uuid4(),
                  expected_content_text='', max_rewrite_count=2)
    values[field] = value
    with pytest.raises(RunSubmissionError): run_submission_intent(**values)


@pytest.mark.asyncio
async def test_default_budget_and_uuid_spelling_identify_the_same_request(book):
    body = request(book, request_id=str(uuid4()).upper())
    del body['max_rewrite_count']
    first = await post(book, body)
    second = await post(book, {**body, 'request_id': body['request_id'].lower(), 'max_rewrite_count': 2})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and first.json()['max_rewrite_count'] == 2
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('stored', [None, ''])
async def test_explicit_empty_original_allows_an_unwritten_chapter(book, stored):
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text=stored))
    body = request(book, expected_content_text='')
    response = await post(book, body)
    assert response.status_code == 200
    with Session(book.engine) as session:
        assert session.get(Chapter, book.cid).content_text == stored
        assert session.get(GenerationRunSubmission, UUID(body['request_id'])) is not None


@pytest.mark.asyncio
async def test_running_duplicate_observes_committed_request_while_model_is_busy(book):
    body = request(book)
    entered, release = asyncio.Event(), asyncio.Event()
    async def draft(*_args):
        entered.set()
        await release.wait()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    book.drafting.side_effect = draft
    first = asyncio.create_task(post(book, body))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        checkpoint = state(book, UUID(body['request_id']))
        receipt = receipt_state(book, body['request_id'])
        calls = list(book.lock_entries)
        second = await post(book, body)
        observed = await get(book, body['request_id'])
        assert second.status_code == observed.status_code == 200
        assert second.json() == observed.json() and second.json()['status'] == 'running'
        assert book.lock_entries == calls
        assert state(book, UUID(body['request_id'])) == checkpoint
        assert receipt_state(book, body['request_id']) == receipt
        book.drafting.assert_awaited_once()
        release.set()
        assert (await first).json()['status'] == 'done'
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', [False, True])
async def test_concurrent_acceptance_between_initial_read_and_lock_is_observed(book, busy):
    body = request(book)
    identity = UUID(body['request_id'])
    async def accepted_elsewhere():
        saved_run(book, id=identity, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
        if busy: book.held.add(book.pid)
    book.before_lock = accepted_elsewhere
    result = await post(book, body)
    assert result.status_code == 200 and result.json()['id'] == body['request_id']
    assert result.json()['phase'] == 'critic'
    book.planning.assert_not_awaited()
    book.drafting.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_acceptance_between_receipt_and_run_reads_is_observed(book, monkeypatch):
    body = request(book)
    identity = UUID(body['request_id'])
    original_get = LocalSession.get
    inserted = False
    async def get_and_accept(db, model, key, **kwargs):
        nonlocal inserted
        row = await original_get(db, model, key, **kwargs)
        if model is GenerationRunSubmission and row is None and not inserted:
            inserted = True
            saved_run(book, id=identity, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
        return row
    monkeypatch.setattr(LocalSession, 'get', get_and_accept)
    result = await post(book, body)
    assert result.status_code == 200, result.text
    assert result.json()['id'] == body['request_id'] and result.json()['phase'] == 'critic'
    assert counts(book) == (1, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['get', 'resume'])
async def test_read_never_mistakes_concurrent_acceptance_for_deleted_run(book, monkeypatch, entry):
    identity = uuid4()
    original_get = LocalSession.get
    inserted = False
    async def get_and_accept(db, model, key, **kwargs):
        nonlocal inserted
        row = await original_get(db, model, key, **kwargs)
        if model is GenerationRun and row is None and not inserted:
            inserted = True
            saved_run(book, id=identity, phase='done', status='done', checkpoint_data={
                'drafting': {'text': DRAFT}, 'finalize': {'final_text': DRAFT},
            })
        return row
    monkeypatch.setattr(LocalSession, 'get', get_and_accept)
    result = await (get(book, identity) if entry == 'get' else resume(book, identity))
    assert result.status_code == 200, result.text
    assert result.json()['id'] == str(identity) and result.json()['status'] == 'done'
    assert counts(book) == (1, 0, 0)
    book.planning.assert_not_awaited()
    book.drafting.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['before_commit', 'after_commit'])
async def test_ambiguous_initial_commit_is_resolved_by_original_id_without_extra_model_work(book, monkeypatch, stage):
    body = request(book)
    identity = UUID(body['request_id'])
    original_commit = LocalSession.commit
    async def uncertain(db):
        if any(isinstance(row, GenerationRunSubmission) for row in db.session.new):
            if stage == 'after_commit': await original_commit(db)
            db.guard.revoke()
            raise ownership.GenerationExecutionUnavailable()
        await original_commit(db)
    monkeypatch.setattr(LocalSession, 'commit', uncertain)
    first = await post(book, body)
    assert first.status_code == 503
    assert first.json()['detail']['run_id'] == body['request_id']
    book.planning.assert_not_awaited()
    book.drafting.assert_not_awaited()
    monkeypatch.setattr(LocalSession, 'commit', original_commit)
    observed = await get(book, identity)
    assert observed.status_code == (200 if stage == 'after_commit' else 404)
    retry = await post(book, body)
    assert retry.status_code == 200, retry.text
    assert retry.json()['id'] == body['request_id']
    if stage == 'after_commit':
        assert retry.json()['phase'] == 'planning'
        book.drafting.assert_not_awaited()
        assert (await resume(book, identity)).json()['status'] == 'done'
    assert counts(book) == (1, 1, 0)
    book.drafting.assert_awaited_once()
    assert receipt_state(book, identity)['id'] == identity


@pytest.mark.asyncio
async def test_failure_after_initial_commit_is_not_reexecuted_by_start_retry(book):
    body = request(book)
    book.drafting.side_effect = RuntimeError('model unavailable')
    first = await post(book, body)
    assert first.status_code == 200 and first.json()['status'] == 'failed'
    receipt = receipt_state(book, body['request_id'])
    second = await post(book, body)
    assert second.status_code == 200 and second.json() == first.json()
    assert receipt_state(book, body['request_id']) == receipt
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
async def test_truncated_candidate_retry_keeps_text_and_never_uses_critic_to_complete_it(book):
    body = request(book)
    book.drafting.return_value = {'text': DRAFT[:-3], 'delivery_status': 'incomplete'}
    first = await post(book, body)
    retry = await post(book, body)
    resumed = await resume(book, UUID(body['request_id']))
    assert first.status_code == retry.status_code == resumed.status_code == 200
    assert first.json() == retry.json() == resumed.json()
    assert resumed.json()['stop_reason'] == 'draft_incomplete'
    book.drafting.assert_awaited_once()
    book.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('content_text', ORIGINAL + '\n末尾作者补写。'), ('title', '改题'),
    ('outline_json', {'steps': ['先还两千元', '保留五百元']}),
    ('chapter_idx', 4), ('global_idx', 9), ('target_word_count', 7500),
    ('status', 'completed'), ('summary', '作者新摘要'), ('summary_source_hash', 'b' * 64),
])
async def test_resume_never_rebases_the_original_chapter_source(book, field, value):
    rid = saved_run(book, phase='critic', rewrite_count=1, max_rewrite_count=1,
                    checkpoint_data={'drafting': {'text': DRAFT}})
    before, receipt = state(book, rid), receipt_state(book, rid)
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values({field: value}))
    response = await resume(book, rid)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_run_source_changed'
    assert state(book, rid) == before and receipt_state(book, rid) == receipt
    book.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_detects_author_edit_then_return_to_same_body(book):
    rid = saved_run(book, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    before = state(book, rid)
    with Session(book.engine) as session:
        chapter = session.get(Chapter, book.cid)
        original_stamp = chapter.updated_at
        chapter.content_text = '另一份编辑稿。'
        chapter.updated_at = original_stamp + timedelta(seconds=1)
        session.commit()
        chapter.content_text = ORIGINAL
        chapter.updated_at = original_stamp + timedelta(seconds=2)
        session.commit()
    response = await resume(book, rid)
    assert response.status_code == 409 and state(book, rid) == before
    book.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_source_is_rechecked_inside_execution_lock(book):
    rid = saved_run(book, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    async def author_edit():
        with book.engine.begin() as connection:
            connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text='等待锁时改稿。'))
    book.before_lock = author_edit
    result = await resume(book, rid)
    assert result.status_code == 409
    book.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_rechecks_full_author_body_inside_execution_lock(book):
    async def author_edit():
        with book.engine.begin() as connection:
            connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text='等待锁时改稿。'))
    book.before_lock = author_edit
    response = await post(book, request(book))
    assert response.status_code == 409 and counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['move_volume', 'move_chapter'])
async def test_resume_preserves_original_volume_identity_and_position(book, change):
    rid = saved_run(book, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    with Session(book.engine) as session:
        chapter = session.get(Chapter, book.cid)
        if change == 'move_volume':
            session.get(Volume, chapter.volume_id).volume_idx = 2
        else:
            volume = Volume(id=uuid4(), project_id=book.pid, title='新卷', volume_idx=2)
            session.add(volume)
            chapter.volume_id = volume.id
        session.commit()
    result = await resume(book, rid)
    assert result.status_code == 409 and result.json()['detail']['code'] == 'generation_run_source_changed'
    book.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing', 'version', 'hash', 'source', 'chapter_hash', 'volume', 'bool_volume', 'budget'])
async def test_missing_or_damaged_submission_cannot_be_rebuilt_during_resume(book, mutation):
    rid = saved_run(book, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    with Session(book.engine) as session:
        receipt = session.get(GenerationRunSubmission, rid)
        source = deepcopy(receipt.source_json)
        if mutation == 'missing': session.delete(receipt)
        elif mutation == 'version': receipt.version = 0
        elif mutation == 'hash': receipt.request_hash = 'x' * 64
        elif mutation == 'source': receipt.source_json = []
        elif mutation == 'chapter_hash':
            source['chapter']['material_hash'] = 'f' * 20
            receipt.source_json = source
        elif mutation == 'volume':
            source['volume']['project_id'] = str(book.other)
            receipt.source_json = source
        elif mutation == 'bool_volume':
            source['volume']['volume_idx'] = True
            # Python True == 1 makes ORM JSON equality treat assignment as
            # unchanged; Core writes the actual malformed stored boolean.
            session.execute(sa.update(GenerationRunSubmission).where(
                GenerationRunSubmission.id == rid).values(source_json=source))
        elif mutation == 'budget': receipt.max_rewrite_count = 3
        session.commit()
    before = state(book, rid)
    response = await resume(book, rid)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_run_source_unverified'
    assert state(book, rid) == before
    book.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [
    {'max_rewrite_count': 8}, {'rewrite_count': -1}, {'rewrite_count': 2},
])
async def test_resume_does_not_accept_a_corrupted_budget(book, values):
    rid = saved_run(book, phase='critic', max_rewrite_count=1, checkpoint_data={'drafting': {'text': DRAFT}})
    with book.engine.begin() as connection:
        connection.execute(sa.update(GenerationRun).where(GenerationRun.id == rid).values(**values))
    before = state(book, rid)
    result = await resume(book, rid)
    assert result.status_code == 409 and state(book, rid) == before
    book.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_run_id_cannot_be_claimed_as_a_new_submission(book):
    rid = saved_run(book, with_submission=False)
    before = state(book, rid)
    response = await post(book, request(book, request_id=str(rid)))
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_request_conflict'
    assert state(book, rid) == before
    book.drafting.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_is_bounded_and_survives_deleting_run_without_reusing_id(book):
    body = request(book)
    response = await post(book, body)
    assert response.status_code == 200
    receipt = receipt_state(book, body['request_id'])
    assert len(json.dumps(receipt['source_json'], ensure_ascii=False)) < 700
    assert ORIGINAL not in json.dumps(receipt['source_json'], ensure_ascii=False)
    with book.engine.begin() as connection:
        connection.execute(sa.delete(GenerationRun).where(GenerationRun.id == UUID(body['request_id'])))
    for result in (await post(book, body), await get(book, body['request_id']), await resume(book, UUID(body['request_id']))):
        assert result.status_code == 410 and result.json()['detail']['code'] == 'generation_run_gone'
        assert result.json()['detail']['run_id'] == body['request_id']
    assert receipt_state(book, body['request_id']) == receipt
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_unconfirmed_database_returns_known_id_and_never_tries_execution(book, monkeypatch):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def unavailable(_caller):
        raise ownership.GenerationExecutionUnavailable()
        yield
    monkeypatch.setattr(runner, 'generation_read_session', unavailable)
    body = request(book)
    for result in (await post(book, body), await get(book, body['request_id'])):
        assert result.status_code == 503 and result.json()['detail']['run_id'] == body['request_id']
    assert book.lock_entries == [] and counts(book) == (0, 0, 0)


@pytest.mark.asyncio
async def test_new_request_after_a_completed_run_is_an_explicit_new_generation(book):
    first = await post(book, request(book))
    second = await post(book, request(book))
    assert first.status_code == second.status_code == 200
    assert first.json()['id'] != second.json()['id']
    assert counts(book) == (2, 2, 0) and book.drafting.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['chapter', 'volume'])
async def test_cascade_deletion_retains_request_and_cannot_resurrect_it(book, target):
    body = request(book)
    assert (await post(book, body)).status_code == 200
    before = receipt_state(book, body['request_id'])
    with book.engine.begin() as connection:
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        chapter = connection.execute(sa.select(Chapter.__table__).where(Chapter.id == book.cid)).mappings().one()
        if target == 'chapter':
            connection.execute(sa.delete(Chapter).where(Chapter.id == book.cid))
        else:
            volume = connection.execute(sa.select(Volume.__table__).where(Volume.id == chapter['volume_id'])).mappings().one()
            connection.execute(sa.delete(Volume).where(Volume.id == volume['id']))
    assert counts(book) == (0, 0, 0)
    assert (await post(book, body)).status_code == 410
    assert receipt_state(book, body['request_id']) == before
    # Reusing the chapter's former UUID cannot recycle the accepted request.
    with book.engine.begin() as connection:
        if target == 'volume': connection.execute(sa.insert(Volume).values(dict(volume)))
        connection.execute(sa.insert(Chapter).values(dict(chapter)))
    assert (await post(book, body)).status_code == 410
    assert (await get(book, body['request_id'])).status_code == 410
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_book_deletion_purges_its_receipts(book):
    body = request(book)
    assert (await post(book, body)).status_code == 200
    with book.engine.begin() as connection:
        connection.execute(sa.delete(Project).where(Project.id == book.pid))
        assert connection.scalar(sa.select(sa.func.count()).select_from(GenerationRunSubmission)) == 0
    assert counts(book) == (0, 0, 0)
    assert (await get(book, body['request_id'])).status_code == 404
    assert (await post(book, body)).status_code == 404
    book.drafting.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonfinite_stored_source_is_rejected_before_creating_a_run(book):
    with book.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(outline_json={'broken': float('nan')}))
    response = await post(book, request(book))
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_run_source_unverified'
    assert counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_capture_in_a_five_million_character_book_only_reads_target_body(book, request):
    from time import perf_counter
    text = ('仅用于离线容量验证的重复文本，不能代表小说质量。' * 240)[:5300]
    identity = uuid4()
    with book.engine.begin() as connection:
        volume_id = connection.scalar(sa.select(Chapter.volume_id).where(Chapter.id == book.cid))
        connection.execute(sa.update(Chapter).where(Chapter.id == book.cid).values(content_text=text))
        rows = [{'id': identity if index == 1005 else uuid4(), 'volume_id': volume_id,
                 'chapter_idx': index, 'global_idx': index, 'title': f'容量第{index}章',
                 'content_text': text, 'outline_json': {'summary': '容量验证'}, 'status': 'draft'}
                for index in range(2, 1006)]
        connection.execute(sa.insert(Chapter), rows)
        total = connection.scalar(sa.select(sa.func.sum(sa.func.length(Chapter.content_text))))
    assert total > 5_000_000
    chapter_reads = []
    def track(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('SELECT') and 'chapters.content_text' in statement:
            chapter_reads.append(statement)
    sa.event.listen(book.engine, 'before_cursor_execute', track)
    started = perf_counter()
    try:
        body = {'request_id': str(uuid4()), 'project_id': str(book.pid), 'chapter_id': str(identity),
                'max_rewrite_count': 0, 'expected_content_text': text}
        response = await post(book, body)
        assert response.status_code == 200, response.text
        reads_after_start = len(chapter_reads)
        assert reads_after_start == 1 and 'chapters.id = ?' in chapter_reads[0]
        retry = await post(book, body)
        assert retry.status_code == 200 and retry.json() == response.json()
        assert len(chapter_reads) == reads_after_start
        assert len(json.dumps(receipt_state(book, body['request_id'])['source_json'])) < 700
    finally:
        sa.event.remove(book.engine, 'before_cursor_execute', track)
    request.node.user_properties.extend([
        ('chapter_count', 1005), ('manuscript_characters', total),
        ('submission_and_retry_seconds', round(perf_counter() - started, 6)),
        ('chapter_body_reads', len(chapter_reads)),
    ])
