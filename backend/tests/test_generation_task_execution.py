"""Actual SQLite transactions behind the independent production wrapper."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, ChapterEvaluation, ChapterVersion, Outline, Project, ReferenceBook, StyleProfile, Volume
from app.services import generation_task_execution as ownership
from app.services.generation_task_execution import owned_task_sessions as actual_writer_sessions
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.generation_submissions import chapter_submission_source
from app.services.manuscript_receipts import manuscript_source
from app.services.production.control import PipelineConflict, content_hash
from app.services.production.fencing import StaleChapterWrite
from app.services.volume_outline_writes import volume_submission_source
from app.tasks import generation_tasks
from tests.support.independent_task_runtime import install_runtime
from tests.test_generation_execution import LocalSession

ORIGINAL = '作者完整保存的原稿。'
DELIVERED = '沈青把两千元放到桌上，另将五百元收回信封。'


@pytest.fixture
def task_book(tmp_path, monkeypatch):
    from app.db import session as database
    engine = sa.create_engine(f'sqlite:///{tmp_path / "tasks.sqlite"}')
    @sa.event.listens_for(engine, 'connect')
    def keys(connection, _record): connection.execute('PRAGMA foreign_keys=ON')
    for model in (Project, Volume, Chapter, GenerationTask, PipelineRun, Outline, ChapterVersion, ChapterEvaluation,
                  ReferenceBook, StyleProfile):
        model.__table__.create(engine)
    pid, vid, cid = uuid4(), uuid4(), uuid4()
    with engine.begin() as db:
        db.execute(sa.insert(Project).values(id=pid, title='执行证明测试', target_word_count=5_000_000))
        db.execute(sa.insert(Volume).values(id=vid, project_id=pid, title='回家', volume_idx=1))
        db.execute(sa.insert(Chapter).values(id=cid, volume_id=vid, title='交钱', chapter_idx=1,
            global_idx=1, content_text=ORIGINAL, outline_json={'summary': '先交钱，再检查弟弟的腿。'}))
    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as db:
            yield LocalSession(db)
    runtime = install_runtime(monkeypatch, engine)
    r = NS(engine=engine, pid=pid, vid=vid, cid=cid, sessions=sessions, runtime=runtime, calls=[],
           core=generation_tasks._run_async_generation_impl)
    async def operation(task_id, *, session_factory, read_session_factory=None):
        r.calls.append((task_id, session_factory, read_session_factory))
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            if task.task_type == 'chapter':
                chapter = await db.get(Chapter, r.cid)
                chapter.content_text, chapter.status, chapter.word_count = DELIVERED, 'completed', len(DELIVERED)
            else:
                db.add(Outline(project_id=pid, level='book', content_json={'raw_text': '已返回的书纲候选。'}))
            task.status, task.result_text = 'completed', DELIVERED
            await db.commit()
    r.operation = operation
    monkeypatch.setattr(database, 'async_session_factory', sessions)
    monkeypatch.setattr(database._state, 'engine', engine)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    yield r
    engine.dispose()


def submit(r, task_type='chapter', **options):
    identity = uuid4()
    with Session(r.engine) as db:
        chapter, volume = db.get(Chapter, r.cid), db.get(Volume, r.vid)
        params = {'chapter_id': str(r.cid), 'chapter_idx': chapter.chapter_idx, 'volume_idx': 1,
            'submission': {'version': 1, 'fingerprint': 'b' * 64,
                'chapter_source': chapter_submission_source(chapter) if task_type == 'chapter' else None}}
        if task_type == 'outline_volume': params['submission']['volume_source'] = volume_submission_source(volume)
        params.update(options)
        db.add(GenerationTask(id=identity, project_id=r.pid, task_type=task_type, params_json=params))
        db.commit()
    return identity


def snapshot(r, identity):
    with Session(r.engine) as db:
        task = db.get(GenerationTask, identity)
        return {column.name: deepcopy(getattr(task, column.name)) for column in task.__table__.columns}


def body(r):
    with Session(r.engine) as db: return db.get(Chapter, r.cid).content_text


async def run(identity):
    return await generation_tasks._run_guarded_generation(str(identity))


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'])
async def test_every_task_type_owns_project_and_keeps_its_first_execution(task_book, task_type):
    r = task_book
    identity = submit(r, task_type)
    original = snapshot(r, identity)['params_json']
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'completed' and state['execution_version'] == 1
    assert state['execution_id'] is not None and state['heartbeat_at'] is not None
    assert state['attempt_count'] == len(state['attempts_json']) == 1
    assert state['attempts_json'][0]['execution_id'] == str(state['execution_id'])
    assert state['execution_params_json'] == original
    assert len(r.calls) == 1 and r.calls[0][1] is not r.calls[0][2]
    assert r.runtime.entries == [r.pid] and not r.runtime.held
    await run(identity)
    assert snapshot(r, identity) == state and len(r.calls) == 1 and r.runtime.entries == [r.pid]
    if task_type == 'chapter':
        proof = state['saved_manuscript_json']
        assert proof['body_hash'] == content_hash(DELIVERED) and proof['word_count'] == len(DELIVERED)
        assert proof['execution_id'] == str(state['execution_id']) and proof['task_id'] == str(identity)
        with Session(r.engine) as db: assert proof['source'] == manuscript_source(db.get(Chapter, r.cid))
    else:
        assert state['saved_manuscript_json'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'])
async def test_busy_project_does_not_consume_an_attempt_for_any_type(task_book, task_type):
    r = task_book
    identity = submit(r, task_type)
    before = snapshot(r, identity)
    r.runtime.held.add(r.pid)
    with pytest.raises(PipelineConflict): await run(identity)
    assert snapshot(r, identity) == before and not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_chapter'])
async def test_active_book_pipeline_excludes_independent_metadata_and_prose(task_book, task_type):
    r = task_book
    identity = submit(r, task_type)
    with Session(r.engine) as db:
        db.add(PipelineRun(project_id=r.pid, state='generating'))
        db.commit()
    with pytest.raises(PipelineConflict): await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'pending' and state['attempt_count'] == 0 and not r.calls


@pytest.mark.asyncio
async def test_duplicate_and_competing_requests_do_not_overlap_live_generation(task_book, monkeypatch):
    r = task_book
    first_id, other_id = submit(r), submit(r, 'outline_book')
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        await r.operation(*args, **kwargs)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', held)
    first = asyncio.create_task(run(first_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert snapshot(r, first_id)['attempt_count'] == 1
        await run(first_id)
        with pytest.raises(PipelineConflict): await run(other_id)
        assert snapshot(r, other_id)['status'] == 'pending'
        release.set()
        await first
        assert len(r.calls) == 1
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_rewinding_status_does_not_reassign_an_existing_execution(task_book):
    r = task_book
    identity = submit(r)
    await run(identity)
    before = snapshot(r, identity)
    with r.engine.begin() as db: db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(status='pending'))
    with pytest.raises(StaleChapterWrite, match='已有执行记录'): await run(identity)
    current = snapshot(r, identity)
    for key in ('execution_id', 'execution_params_json', 'attempt_count', 'attempts_json', 'saved_manuscript_json'):
        assert current[key] == before[key]
    assert len(r.calls) == 1 and body(r) == DELIVERED


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book', 'outline_volume'])
async def test_legacy_missing_submission_is_not_reconstructed_at_execution(task_book, task_type):
    r = task_book
    identity = submit(r, task_type, submission={})
    with pytest.raises(StaleChapterWrite, match='缺少原始提交'): await run(identity)
    state = snapshot(r, identity)
    assert state['execution_version'] == 0 and state['attempt_count'] == 0 and state['status'] == 'failed'
    assert not r.calls and body(r) == ORIGINAL


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['before', 'after'])
async def test_unconfirmed_claim_never_starts_model_or_guesses_committed_outcome(task_book, monkeypatch, stage):
    r = task_book
    identity = submit(r)
    original = r.runtime.OwnedSession.commit
    async def lose(db):
        if stage == 'after': await original(db)
        db.guard.revoke()
        raise GenerationExecutionUnavailable()
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', lose)
    with pytest.raises(GenerationExecutionUnavailable): await run(identity)
    assert not r.calls and body(r) == ORIGINAL
    state = snapshot(r, identity)
    assert state['status'] == ('running' if stage == 'after' else 'pending')
    assert state['attempt_count'] == (1 if stage == 'after' else 0)
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', original)
    if stage == 'after':
        await run(identity)
        assert not r.calls and snapshot(r, identity) == state
    else:
        await run(identity)
        assert len(r.calls) == 1 and snapshot(r, identity)['attempt_count'] == 1


@pytest.mark.asyncio
async def test_lost_save_ack_preserves_committed_body_and_atomic_proof(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    original = r.runtime.OwnedSession.commit
    async def lose(db):
        body_write = any(isinstance(row, Chapter) for row in db.session.dirty)
        await original(db)
        if body_write:
            db.guard.revoke()
            raise GenerationExecutionUnavailable()
    monkeypatch.setattr(r.runtime.OwnedSession, 'commit', lose)
    with pytest.raises(GenerationExecutionUnavailable): await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'completed' and body(r) == DELIVERED
    assert state['saved_manuscript_json']['body_hash'] == content_hash(DELIVERED)
    await run(identity)
    assert snapshot(r, identity) == state and len(r.calls) == 1


@pytest.mark.asyncio
async def test_failed_save_proof_rolls_back_body_in_the_same_transaction(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    def failed(*_args): raise RuntimeError('receipt storage failed')
    monkeypatch.setattr('app.services.generation_task_receipts.record_task_save', failed)
    with pytest.raises(GenerationExecutionUnavailable): await run(identity)
    state = snapshot(r, identity)
    assert body(r) == ORIGINAL and state['status'] == 'running' and state['saved_manuscript_json'] is None
    assert state['attempt_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['orm', 'core'])
async def test_cancelled_actor_cannot_write_prose_or_metadata(task_book, monkeypatch, mode):
    r = task_book
    identity = submit(r, 'chapter' if mode == 'orm' else 'outline_book')
    entered, release = asyncio.Event(), asyncio.Event()
    async def operation(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            chapter = await db.get(Chapter, r.cid)
            entered.set()
            await release.wait()
            if mode == 'orm': chapter.content_text = '取消后迟到的正文。'
            else: await db.execute(sa.insert(Outline).values(project_id=r.pid, level='book', content_json={'raw_text': '取消后书纲'}))
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with Session(r.engine) as author:
            row = author.get(GenerationTask, identity)
            row.status = 'cancelled'
            row.params_json = {**row.params_json, 'cancelled_at': '2026-10-09'}
            author.commit()
        release.set()
        with pytest.raises((GenerationExecutionUnavailable, ownership.TaskExecutionLost)): await pending
        assert snapshot(r, identity)['status'] == 'cancelled' and body(r) == ORIGINAL
        with Session(r.engine) as db: assert db.scalar(sa.select(sa.func.count()).select_from(Outline)) == 0
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['orm', 'core'])
async def test_read_session_cannot_be_used_as_a_salvage_writer(task_book, monkeypatch, mode):
    r = task_book
    identity = submit(r, 'outline_book')
    async def operation(task_id, *, session_factory, read_session_factory):
        async with read_session_factory() as db:
            if mode == 'orm':
                db.add(Outline(project_id=r.pid, level='book', content_json={'raw_text': '旁路'}))
                await db.commit()
            else:
                await db.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(content_text='旁路正文'))
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    with pytest.raises(ownership.TaskExecutionLost, match='持锁连接'): await run(identity)
    assert body(r) == ORIGINAL
    with Session(r.engine) as db: assert db.scalar(sa.select(sa.func.count()).select_from(Outline)) == 0


@pytest.mark.asyncio
async def test_closed_execution_factory_cannot_write_after_the_worker_has_returned(task_book):
    r = task_book
    identity = submit(r)
    await run(identity)
    state = snapshot(r, identity)
    with pytest.raises(GenerationExecutionUnavailable):
        async with r.calls[0][1]() as db:
            await db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(status='failed'))
    assert snapshot(r, identity) == state


@pytest.mark.asyncio
async def test_read_context_is_separate_and_original_params_survive_runtime_diagnostics(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    original = snapshot(r, identity)['params_json']
    async def operation(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            task.params_json = {**task.params_json, 'runtime_diagnostic': {'text': '评审未完成'}}
            await db.commit()
            async with read_session_factory() as reader:
                assert (await reader.get(Chapter, r.cid)).content_text == ORIGINAL
            task.status = 'needs_review'
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    await run(identity)
    state = snapshot(r, identity)
    assert state['execution_params_json'] == original
    assert 'runtime_diagnostic' in state['params_json'] and state['status'] == 'needs_review'


@pytest.mark.asyncio
async def test_heartbeat_updates_only_its_current_execution_without_reordering_task_progress(task_book, monkeypatch):
    r = task_book
    identity = submit(r, 'outline_book')
    entered, release = asyncio.Event(), asyncio.Event()
    async def operation(*args, **kwargs):
        entered.set()
        await release.wait()
        await r.operation(*args, **kwargs)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', 0.01)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = snapshot(r, identity)
        async with asyncio.timeout(2):
            while snapshot(r, identity)['heartbeat_at'] == before['heartbeat_at']:
                await asyncio.sleep(0.01)
        current = snapshot(r, identity)
        assert current['updated_at'] == before['updated_at'] and current['execution_id'] == before['execution_id']
        assert current['status'] == 'running' and current['progress_text'] == before['progress_text']
        release.set()
        await pending
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_sessions_refuse_overlapping_transactions(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    async def operation(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            await db.get(Chapter, r.cid)
            with pytest.raises(ownership.TaskExecutionLost, match='事务'):
                async with session_factory():
                    pytest.fail('overlapping write session was admitted')
            await db.rollback()
        await r.operation(task_id, session_factory=session_factory, read_session_factory=read_session_factory)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    await run(identity)
    assert snapshot(r, identity)['status'] == 'completed' and body(r) == DELIVERED


@pytest.mark.asyncio
@pytest.mark.parametrize('cause', ['heartbeat_error', 'cancelled', 'changed_execution'])
async def test_heartbeat_failure_revokes_late_writes_and_drains_generation(task_book, monkeypatch, cause):
    r = task_book
    identity = submit(r)
    entered, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def operation(task_id, *, session_factory, read_session_factory):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
            with pytest.raises(GenerationExecutionUnavailable):
                await r.operation(task_id, session_factory=session_factory, read_session_factory=read_session_factory)
    async def broken(owner, sessions, stop):
        await entered.wait()
        raise RuntimeError('offline heartbeat failure')
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', 0.01)
    if cause == 'heartbeat_error': monkeypatch.setattr(ownership, '_heartbeat', broken)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cause != 'heartbeat_error':
            with Session(r.engine) as db:
                task = db.get(GenerationTask, identity)
                if cause == 'cancelled':
                    task.status = 'cancelled'
                    task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
                else:
                    task.execution_id = uuid4()
                db.commit()
        await asyncio.wait_for(interrupted.wait(), 2)
        assert not pending.done() and r.pid in r.runtime.held
        before = snapshot(r, identity)
        # Repeated cancellation cannot release the connection while the
        # model coroutine still has a cleanup/salvage continuation.
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and r.pid in r.runtime.held
        release.set()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await pending
        assert snapshot(r, identity) == before and body(r) == ORIGINAL
        assert r.pid not in r.runtime.held
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pipeline_child_redelivery_is_read_only_for_the_independent_worker(task_book):
    r = task_book
    identity = submit(r, pipeline_id=str(uuid4()))
    before = snapshot(r, identity)
    await run(identity)
    assert snapshot(r, identity) == before and not r.calls and not r.runtime.entries


@pytest.mark.asyncio
async def test_rejected_foreign_task_cannot_be_failed_under_another_projects_lock(task_book):
    r = task_book
    identity, other_project = submit(r), uuid4()
    with r.engine.begin() as db:
        db.execute(sa.insert(Project).values(id=other_project, title='另一作品'))
    before = snapshot(r, identity)
    with pytest.raises(StaleChapterWrite, match='归属'):
        await ownership.execute_independent_task(identity, other_project,
            sessions=r.sessions, generate=lambda writers, readers: r.operation(
                identity, session_factory=writers, read_session_factory=readers))
    assert snapshot(r, identity) == before and not r.calls


@pytest.mark.asyncio
async def test_cancellation_wins_before_a_rejected_claim_records_failure(task_book, monkeypatch):
    r = task_book
    identity = submit(r, submission={})
    original = r.runtime.OwnedSession.get
    cancelled = []
    def cancel():
        with Session(r.engine) as db:
            row = db.get(GenerationTask, identity)
            row.status = 'cancelled'
            row.params_json = {**row.params_json, 'cancelled_at': '2026-10-09'}
            db.commit()
        cancelled.append(True)
    async def read(db, model, key, **kwargs):
        if model is GenerationTask and not cancelled:
            # Model both PG lock schedules. With a lock the cancellation
            # completes before the locked read; without one it can commit
            # after that read and expose the stale overwrite.
            locked = bool(kwargs.get('with_for_update'))
            if locked: Context().run(cancel)
            row = await original(db, model, key, **kwargs)
            if not locked: Context().run(cancel)
            return row
        return await original(db, model, key, **kwargs)
    monkeypatch.setattr(r.runtime.OwnedSession, 'get', read)
    with pytest.raises(StaleChapterWrite): await run(identity)
    state = snapshot(r, identity)
    assert cancelled and state['status'] == 'cancelled' and state['params_json']['cancelled_at']
    assert state['attempt_count'] == 0 and not r.calls


@pytest.mark.asyncio
async def test_heartbeat_stops_a_child_when_the_write_connection_is_known_lost(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    entered, drained = asyncio.Event(), asyncio.Event()
    async def hold(*a, **k):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', hold)
    monkeypatch.setattr(ownership, 'HEARTBEAT_SECONDS', .01)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        r.runtime.sessions[0].guard.revoke()
        before = snapshot(r, identity)
        with pytest.raises(GenerationExecutionUnavailable):
            await asyncio.wait_for(asyncio.shield(pending), 1)
        assert drained.is_set() and snapshot(r, identity) == before and not r.runtime.held
    finally:
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_return_after_only_a_manuscript_save_is_explicitly_needs_review(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    async def save_only(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            chapter = await db.get(Chapter, r.cid)
            chapter.content_text, chapter.status = DELIVERED, 'completed'
            await db.commit()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', save_only)
    await run(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'needs_review' and state['saved_manuscript_json'] is not None
    assert body(r) == DELIVERED and not state['result_text']


@pytest.mark.asyncio
async def test_nested_write_cannot_reuse_outer_ownership_verification(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    async def nested(task_id, *, session_factory, read_session_factory):
        async with session_factory() as db:
            # Verify once in the root transaction, then try a savepoint.
            await db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(progress_text='准备完成。'))
            savepoint = db.session.begin_nested()
            with pytest.raises(ownership.TaskExecutionLost, match='嵌套'):
                await db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(result_text='保存点候选。'))
            savepoint.rollback()
            await db.commit()
        await r.operation(task_id, session_factory=session_factory, read_session_factory=read_session_factory)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', nested)
    await run(identity)
    assert snapshot(r, identity)['status'] == 'completed' and body(r) == DELIVERED


@pytest.mark.asyncio
async def test_clean_heartbeat_exit_does_not_expose_work_to_repeated_outer_cancellation(task_book, monkeypatch):
    r = task_book
    identity = submit(r)
    entered, pulse_done, cleaning, release = (asyncio.Event() for _ in range(4))
    async def operation(*a, **k):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    async def terminal_heartbeat(*a, **k):
        await entered.wait()
        pulse_done.set()
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    monkeypatch.setattr(ownership, '_heartbeat', terminal_heartbeat)
    pending = asyncio.create_task(run(identity))
    try:
        await asyncio.wait_for(pulse_done.wait(), 2)
        for _ in range(3): await asyncio.sleep(0)
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        pending.cancel()
        for _ in range(5): await asyncio.sleep(0)
        assert not pending.done() and r.pid in r.runtime.held
        release.set()
        with pytest.raises(asyncio.CancelledError): await pending
        assert r.pid not in r.runtime.held
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_each_owned_session_drains_its_close_before_returning_after_cancellation(monkeypatch):
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close_tasks = []
    class SessionClose:
        # Exercise the actual SQLAlchemy context-exit behavior with an
        # explicitly delayed driver close, without any live connection.
        __aenter__ = AsyncSession.__aenter__
        __aexit__ = AsyncSession.__aexit__
        def __init__(self, **kwargs): pass
        async def close(self):
            close_tasks.append(asyncio.current_task())
            closing.set()
            await release.wait()
            closed.set()
    monkeypatch.setattr(ownership, 'GenerationExecutionSession', SessionClose)
    parent = NS(guard=NS(check=lambda: None), bind=NS(in_transaction=lambda: False))
    async def use():
        async with actual_writer_sessions(parent)():
            pass
    pending = asyncio.create_task(use())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        pending.cancel()
        for _ in range(5): await asyncio.sleep(0)
        assert not pending.done() and not closed.is_set()
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError): await pending
        assert closed.is_set() and all(child.done() for child in close_tasks)
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, *close_tasks, return_exceptions=True)
