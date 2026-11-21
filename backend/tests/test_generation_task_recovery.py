"""Recover through the real execution wrapper and SQLite transactions.

Only the physical PG transport is substituted by the existing explicit test
adapter. A cancelled coroutine models a worker exiting without a terminal save.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, Outline, Project, Volume
from app.services import generation_task_execution as ownership
from app.services.generation_execution import GenerationExecutionUnavailable, PhysicalConnectionGuard
from app.services.generation_task_recovery import STATUS_PROGRESS, recover_task_execution
from app.services.production.control import PipelineConflict
from app.tasks import generation_tasks
from tests.test_generation_task_execution import DELIVERED, ORIGINAL, body, run, snapshot, submit, task_book


@pytest.fixture
def interrupted_task(task_book, monkeypatch):
    r = task_book
    r.interrupted = []
    async def interrupt(task_id, **kwargs):
        r.interrupted.append(task_id)
        raise asyncio.CancelledError()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', interrupt)
    return r


def expire(r, identity, **values):
    with r.engine.begin() as db:
        db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(
            heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=120),
            updated_at=GenerationTask.updated_at, **values))


async def abandon(r, task_type='chapter', **params):
    identity = submit(r, task_type, **params)
    with pytest.raises(asyncio.CancelledError):
        await run(identity)
    expire(r, identity)
    return identity


async def recover(identity):
    return await generation_tasks._run_guarded_generation(str(identity), recover=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'])
async def test_only_explicit_recovery_can_retry_three_total_attempts_with_the_same_request(interrupted_task, task_type):
    r = interrupted_task
    identity = await abandon(r, task_type, max_revise_rounds=2)
    first = snapshot(r, identity)
    assert first['execution_source_json']['task_type'] == task_type
    await run(identity)
    assert snapshot(r, identity) == first and len(r.interrupted) == 1
    for attempt in (2, 3):
        with pytest.raises(asyncio.CancelledError): await recover(identity)
        current = snapshot(r, identity)
        assert current['status'] == 'running' and current['attempt_count'] == attempt
        assert len(current['attempts_json']) == attempt and len(current['recovery_history_json']) == attempt - 1
        assert current['execution_id'] != first['execution_id']
        assert current['execution_params_json'] == first['execution_params_json']
        assert current['execution_source_json'] == first['execution_source_json']
        assert current['params_json'] == first['params_json']
        expire(r, identity)
    before = snapshot(r, identity)
    await recover(identity)
    final = snapshot(r, identity)
    assert final['status'] == 'needs_review' and final['attempt_count'] == 3
    assert final['attempts_json'] == before['attempts_json']
    assert final['recovery_history_json'][:-1] == before['recovery_history_json']
    assert final['recovery_history_json'][-1]['outcome'] == 'budget_exhausted'
    assert len(r.interrupted) == 3 and body(r) == ORIGINAL
    await recover(identity)
    assert snapshot(r, identity) == final


@pytest.mark.asyncio
@pytest.mark.parametrize('message', sorted(STATUS_PROGRESS))
async def test_known_typed_status_is_not_a_retained_candidate(interrupted_task, message):
    r = interrupted_task
    identity = await abandon(r)
    expire(r, identity, progress_text=message, progress_kind='status', char_count=0)
    with pytest.raises(asyncio.CancelledError): await recover(identity)
    state = snapshot(r, identity)
    assert state['attempt_count'] == 2 and state['progress_text'] == message
    assert state['progress_kind'] == 'status' and body(r) == ORIGINAL


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [
    {'progress_text': '他刚从信封里取出，', 'progress_kind': 'candidate'},
    {'progress_text': '他刚从信封里取出，', 'progress_kind': 'status'},
    {'progress_text': next(iter(STATUS_PROGRESS)), 'progress_kind': None},
    {'progress_text': '旧系统留下的未知文字', 'progress_kind': None},
    {'progress_text': '', 'progress_kind': 'candidate'},
    {'progress_kind': 'unknown'}, {'char_count': -1},
    {'result_text': DELIVERED}, {'polished_text': DELIVERED},
])
async def test_partial_or_uncertain_delivery_is_preserved_for_review(interrupted_task, values):
    r = interrupted_task
    identity = await abandon(r)
    expire(r, identity, **values)
    before = snapshot(r, identity)
    await recover(identity)
    after = snapshot(r, identity)
    assert after['status'] == 'needs_review' and after['attempt_count'] == 1
    assert after['recovery_history_json'][-1]['outcome'] == 'candidate_requires_review'
    for key in ('progress_text', 'progress_kind', 'result_text', 'polished_text', 'char_count', 'execution_params_json'):
        assert after[key] == before[key]
    assert len(r.interrupted) == 1 and body(r) == ORIGINAL


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['body', 'outline', 'position', 'volume', 'book', 'settings', 'delete_target'])
async def test_author_source_changes_stop_recovery_without_overwriting_edits(interrupted_task, change):
    r = interrupted_task
    identity = await abandon(r)
    with r.engine.begin() as db:
        if change in {'body', 'outline', 'position'}:
            values = {'body': {'content_text': '作者明确改过的完整新稿。'},
                'outline': {'outline_json': {'summary': '先照看弟弟，不交钱。'}},
                'position': {'chapter_idx': 2, 'global_idx': 2}}[change]
            db.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(**values))
        elif change == 'volume':
            db.execute(sa.update(Volume).where(Volume.id == r.vid).values(volume_idx=2))
        elif change == 'book':
            db.execute(sa.insert(Outline).values(project_id=r.pid, level='book', content_json={'raw_text': '作者的新书纲。'}))
        elif change == 'settings':
            db.execute(sa.update(Project).where(Project.id == r.pid).values(settings_json={'ending': '兄弟分家'}))
        else:
            db.execute(sa.delete(Chapter).where(Chapter.id == r.cid))
    await recover(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_review' and state['attempt_count'] == 1
    assert state['recovery_history_json'][-1]['outcome'] == 'source_changed'
    assert len(r.interrupted) == 1
    if change == 'body': assert body(r) == '作者明确改过的完整新稿。'


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['outline_book', 'outline_chapter', 'outline_volume'])
async def test_outline_target_changes_are_protected_too(interrupted_task, task_type):
    r = interrupted_task
    identity = await abandon(r, task_type)
    with r.engine.begin() as db:
        if task_type == 'outline_chapter':
            db.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(outline_json={'summary': '作者修改后的章纲。'}))
        elif task_type == 'outline_volume':
            db.execute(sa.update(Volume).where(Volume.id == r.vid).values(summary='作者修改后的卷简介。'))
        else:
            db.execute(sa.insert(Outline).values(project_id=r.pid, level='book', content_json={'raw_text': '作者修改后的书纲。'}))
    await recover(identity)
    assert snapshot(r, identity)['recovery_history_json'][-1]['outcome'] == 'source_changed'
    assert len(r.interrupted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing_source', 'params', 'unknown_param', 'nan_param', 'attempt_hash',
    'attempt_id', 'attempt_time', 'count', 'history_object', 'history_entry'])
async def test_legacy_and_damaged_execution_proof_is_not_reconstructed(interrupted_task, damage):
    r = interrupted_task
    identity = await abandon(r)
    with Session(r.engine) as db:
        row = db.get(GenerationTask, identity)
        if damage == 'missing_source': row.execution_source_json = None
        elif damage == 'params': row.params_json = {**row.params_json, 'chapter_idx': 99}
        elif damage == 'unknown_param': row.params_json = {**row.params_json, 'new_instruction': '改写结局'}
        elif damage == 'nan_param':
            row.params_json = {**row.params_json, 'extra': float('nan')}
            row.execution_params_json = deepcopy(row.params_json)
        elif damage == 'count': row.attempt_count = 2
        elif damage == 'history_object': row.recovery_history_json = {'legacy': '必须保留的损坏记录'}
        elif damage == 'history_entry': row.recovery_history_json = [{'outcome': 'unverified'}]
        else:
            item = deepcopy(row.attempts_json[0])
            item[{'attempt_hash': 'params_hash', 'attempt_id': 'execution_id', 'attempt_time': 'started_at'}[damage]] = 'broken'
            row.attempts_json = [item]
        db.commit()
    before = snapshot(r, identity)
    await recover(identity)
    after = snapshot(r, identity)
    assert after['status'] == 'needs_review' and after['attempt_count'] == before['attempt_count']
    assert after['attempts_json'] == before['attempts_json'] and len(r.interrupted) == 1
    assert after['execution_source_json'] == before['execution_source_json']
    if damage == 'history_object':
        assert after['recovery_history_json'] == before['recovery_history_json']
    else:
        assert after['recovery_history_json'][:-1] == before['recovery_history_json']


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['live_heartbeat', 'cancelled', 'marked_cancelled', 'old_version', 'no_heartbeat',
    'no_execution', 'completed', 'batch', 'pipeline', 'deleted_project'])
async def test_ineligible_tasks_are_read_only(interrupted_task, condition):
    r = interrupted_task
    identity = await abandon(r)
    with Session(r.engine) as db:
        row = db.get(GenerationTask, identity)
        if condition == 'live_heartbeat': row.heartbeat_at = datetime.now(timezone.utc)
        elif condition in {'cancelled', 'completed'}: row.status = condition
        elif condition == 'old_version': row.execution_version = 0
        elif condition == 'no_heartbeat': row.heartbeat_at = None
        elif condition == 'no_execution': row.execution_id = None
        elif condition == 'deleted_project': db.get(Project, r.pid).deleted_at = datetime.now(timezone.utc)
        else:
            key = {'marked_cancelled': 'cancelled_at', 'batch': 'batch_request_id', 'pipeline': 'pipeline_id'}[condition]
            row.params_json = {**row.params_json, key: str(uuid4())}
        db.commit()
    before = snapshot(r, identity)
    if condition == 'deleted_project':
        from app.services.production.fencing import StaleChapterWrite
        with pytest.raises(StaleChapterWrite): await recover(identity)
    else:
        await recover(identity)
    assert snapshot(r, identity) == before and len(r.interrupted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['physical_lock', 'book_pipeline'])
async def test_expired_heartbeat_does_not_grant_execution_ownership(interrupted_task, conflict):
    r = interrupted_task
    identity = await abandon(r)
    if conflict == 'physical_lock': r.runtime.held.add(r.pid)
    else:
        with r.engine.begin() as db: db.execute(sa.insert(PipelineRun).values(project_id=r.pid, state='generating'))
    before = snapshot(r, identity)
    with pytest.raises(PipelineConflict): await recover(identity)
    assert snapshot(r, identity) == before and len(r.interrupted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', ['before', 'after'])
async def test_uncertain_recovery_commit_never_calls_model_or_resets_the_budget(interrupted_task, monkeypatch, ack):
    r = interrupted_task
    identity = await abandon(r)
    before = snapshot(r, identity)
    commit = r.runtime.OwnedSession.commit
    async def lose(db):
        if ack == 'after': await commit(db)
        db.guard.revoke()
        raise GenerationExecutionUnavailable()
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', lose)
    with pytest.raises(GenerationExecutionUnavailable): await recover(identity)
    current = snapshot(r, identity)
    assert len(r.interrupted) == 1 and body(r) == ORIGINAL
    if ack == 'before': assert current == before
    else:
        assert current['attempt_count'] == 2 and len(current['recovery_history_json']) == 1
        assert current['execution_params_json'] == before['execution_params_json']
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', commit)
    await run(identity)
    assert snapshot(r, identity) == current and len(r.interrupted) == 1


@pytest.mark.asyncio
async def test_recovery_helper_refuses_a_plain_session_even_when_rows_look_stale(interrupted_task):
    r = interrupted_task
    identity = await abandon(r)
    before = snapshot(r, identity)
    async with r.sessions() as db:
        with pytest.raises(GenerationExecutionUnavailable): await recover_task_execution(db, identity, r.pid)
    assert snapshot(r, identity) == before


@pytest.mark.asyncio
async def test_saved_manuscript_receipt_stays_historical_after_quarantine(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    async def interrupted_after_save(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            chapter = await db.get(Chapter, r.cid)
            chapter.content_text, chapter.status, chapter.word_count = DELIVERED, 'completed', len(DELIVERED)
            await db.commit()
        raise asyncio.CancelledError()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', interrupted_after_save)
    with pytest.raises(asyncio.CancelledError): await run(identity)
    expire(r, identity)
    before = snapshot(r, identity)
    assert before['saved_manuscript_json'] is not None and not before['result_text']
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', r.operation)
    await recover(identity)
    after = snapshot(r, identity)
    assert body(r) == DELIVERED and not r.calls
    assert after['status'] == 'needs_review' and after['attempt_count'] == 1
    assert after['saved_manuscript_json'] == before['saved_manuscript_json']
    assert after['execution_id'] != before['execution_id']
    assert after['recovery_history_json'][-1]['outcome'] == 'saved_requires_review'
    await recover(identity)
    assert snapshot(r, identity) == after


@pytest.mark.asyncio
async def test_only_one_of_two_recovery_deliveries_starts_a_successor(interrupted_task, monkeypatch):
    r = interrupted_task
    identity = await abandon(r)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(*args, **kwargs):
        entered.set()
        await release.wait()
        await r.operation(*args, **kwargs)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', hold)
    first = asyncio.create_task(recover(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await recover(identity)
        assert snapshot(r, identity)['attempt_count'] == 2
        release.set()
        await first
        assert len(r.calls) == 1 and snapshot(r, identity)['status'] == 'completed'
        assert body(r) == DELIVERED
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['manuscript', 'outline', 'orm_result', 'core_failure'])
async def test_old_execution_uuid_cannot_save_after_a_successor_is_committed(interrupted_task, mode):
    r = interrupted_task
    identity = await abandon(r)
    previous_id = snapshot(r, identity)['execution_id']
    with pytest.raises(asyncio.CancelledError): await recover(identity)
    before = snapshot(r, identity)
    with r.engine.connect() as connection:
        guard = PhysicalConnectionGuard(connection)
        try:
            old = ownership.TaskExecution(identity, r.pid, previous_id, guard)
            with ownership.task_execution(old):
                with Session(connection) as db:
                    with pytest.raises(ownership.TaskExecutionLost):
                        if mode == 'manuscript': db.get(Chapter, r.cid).content_text = '旧执行返回的迟到正文。'
                        elif mode == 'outline': db.add(Outline(project_id=r.pid, level='book', content_json={'raw_text': '迟到书纲。'}))
                        elif mode == 'orm_result': db.get(GenerationTask, identity).result_text = '迟到候选。'
                        else: db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(status='failed'))
                        db.commit()
        finally:
            guard.revoke()
            guard.detach()
    assert snapshot(r, identity) == before and body(r) == ORIGINAL


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancelled', 'new_heartbeat', 'foreign_project'])
async def test_stale_discovery_is_rechecked_inside_the_real_execution_lease(interrupted_task, monkeypatch, change):
    r = interrupted_task
    identity = await abandon(r)
    lease, observed = ownership.generation_execution_session, {}
    @asynccontextmanager
    async def raced(selector, project_id):
        with r.engine.begin() as db:
            values = {'cancelled': {'status': 'cancelled'},
                'new_heartbeat': {'heartbeat_at': datetime.now(timezone.utc)},
                'foreign_project': {'project_id': uuid4()}}[change]
            db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(**values))
        observed.update(snapshot(r, identity))
        async with lease(selector, project_id) as db: yield db
    monkeypatch.setattr(ownership, 'generation_execution_session', raced)
    await recover(identity)
    assert snapshot(r, identity) == observed and len(r.interrupted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [
    {'progress_text': '旧执行已经返回的片段。'}, {'result_text': DELIVERED},
    {'polished_text': DELIVERED}, {'saved_manuscript_json': {'legacy_proof': '保留'}},
])
async def test_pending_legacy_output_is_not_misclassified_as_a_new_empty_attempt(task_book, values):
    r = task_book
    identity = submit(r)
    with r.engine.begin() as db:
        db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(**values))
    await recover(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_review' and state['attempt_count'] == 0
    assert state['execution_source_json'] is None and state['execution_id'] is None
    assert all(state[key] == value for key, value in values.items())
    assert body(r) == ORIGINAL and not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [
    {'attempts_json': [{'execution_id': str(uuid4())}]},
    {'execution_params_json': {'original': 'unverified'}},
    {'execution_source_json': {'original': 'unverified'}},
    {'recovery_history_json': [{'original': 'unverified'}]},
])
async def test_partial_claim_metadata_cannot_be_erased_by_a_pending_retry(task_book, values):
    from app.services.production.fencing import StaleChapterWrite
    r = task_book
    identity = submit(r)
    with r.engine.begin() as db:
        db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(**values))
    with pytest.raises(StaleChapterWrite, match='已有执行记录'): await recover(identity)
    state = snapshot(r, identity)
    assert state['attempt_count'] == 0 and state['execution_id'] is None
    assert all(state[key] == value for key, value in values.items())
    assert body(r) == ORIGINAL and not r.calls
