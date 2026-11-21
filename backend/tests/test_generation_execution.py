"""Real SQLite state/HTTP checks and real SQLAlchemy connection invalidation.

Only the PG advisory operation and async driver transport are substituted.
These cases do not certify PostgreSQL worker or network-failure behavior; the
companion external-services tests exercise those boundaries with real PG.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, registry

from app.api import generation_runs
from app.db.session import get_db
from app.models.generation_run import CriticReport, GenerationRun, GenerationRunSubmission
from app.models.pipeline import PipelineRun
from app.models.project import Chapter, ChapterVariant, Project, Volume
from app.services import generation_execution as ownership, generation_runner as runner
from app.services.production.control import PipelineConflict
from app.services.review_contract import make_critic_report
from app.services.generation_run_submissions import make_run_submission, run_submission_intent
from tests.support.generation_run_fixture import verified_history


DRAFT = '沈青数出两千元，等弟弟伸手来接。'


class LocalSession:
    """Public synchronous SQLAlchemy, adapted explicitly for offline tests."""
    def __init__(self, session): self.session = session
    @property
    def no_autoflush(self): return self.session.no_autoflush
    @property
    def dirty(self): return self.session.dirty
    def add(self, row): self.session.add(row)
    async def get(self, model, key, **kwargs):
        return self.session.get(model, UUID(str(key)), **kwargs)
    async def execute(self, statement, *args, **kwargs):
        return self.session.execute(statement, *args, **kwargs)
    async def scalar(self, statement): return self.session.scalar(statement)
    async def commit(self): self.session.commit()
    async def rollback(self): self.session.rollback()
    async def refresh(self, row): self.session.refresh(row)
    async def flush(self): self.session.flush()


@pytest.fixture
def book(tmp_path, monkeypatch):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "execution.sqlite"}')
    @sa.event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
    for model in (Project, Volume, Chapter, GenerationRun, GenerationRunSubmission, CriticReport, ChapterVariant, PipelineRun):
        model.__table__.create(engine)
    with Session(engine) as seed:
        project = Project(id=uuid4(), title='执行权测试')
        other = Project(id=uuid4(), title='另一作品')
        volume = Volume(id=uuid4(), project_id=project.id, volume_idx=1, title='归来')
        chapter = Chapter(id=uuid4(), volume_id=volume.id, chapter_idx=1, global_idx=1, title='交付',
                          content_text='作者保存的原稿。')
        seed.add_all([project, other, volume, chapter])
        seed.commit()
        r = NS(engine=engine, pid=project.id, other=other.id, cid=chapter.id,
               held=set(), lock_entries=[], readers=0, before_lock=None)

    @asynccontextmanager
    async def read_session(_caller):
        r.readers += 1
        with Session(engine, expire_on_commit=False) as session:
            yield LocalSession(session)

    @asynccontextmanager
    async def execution_session(_caller, project_id):
        pid = UUID(str(project_id))
        r.lock_entries.append(pid)
        if r.before_lock is not None:
            before = r.before_lock
            r.before_lock = None
            await before()
        if pid in r.held:
            raise PipelineConflict('本书已有任务。')
        r.held.add(pid)
        try:
            with engine.connect() as connection:
                guard = ownership.PhysicalConnectionGuard(connection)
                try:
                    with Session(bind=connection, expire_on_commit=False) as session:
                        db = LocalSession(session)
                        db.guard = guard
                        yield db
                finally:
                    guard.revoke()
                    connection.invalidate()
                    guard.detach()
        finally:
            r.held.remove(pid)

    monkeypatch.setattr(runner, 'generation_read_session', read_session)
    monkeypatch.setattr(runner, 'generation_execution_session', execution_session)
    r.planning = AsyncMock(return_value={'pack': {'system_prompt': '核实后的当前前文'}})
    r.drafting = AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'})
    async def review(**kwargs):
        return make_critic_report(kwargs['draft'], issues=[], stages=[{'name': 'critic', 'status': 'complete'}])
    r.review = AsyncMock(side_effect=review)
    monkeypatch.setattr(runner, '_phase_planning', r.planning)
    monkeypatch.setattr(runner, '_phase_drafting', r.drafting)
    monkeypatch.setattr(runner, 'run_critic', r.review)
    monkeypatch.setattr(runner.run_bus, 'publish', AsyncMock())
    monkeypatch.setenv('BVSR_ENABLED', 'false')
    monkeypatch.setenv('AGENT_TOOL_LOOP_ENABLED', 'false')
    async def database():
        # Entrypoints may use this only to select the engine. A refresh or
        # commit on this borrowed object must never be needed for execution.
        yield NS()
    app = FastAPI()
    app.include_router(generation_runs.router)
    app.dependency_overrides[get_db] = database
    r.client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test',
    )
    yield r
    engine.dispose()


def saved_run(r, *, with_submission=True, with_history=True, **values):
    defaults = dict(id=uuid4(), project_id=r.pid, chapter_id=r.cid, status='running',
                    phase='drafting', rewrite_count=0, max_rewrite_count=0,
                    checkpoint_data={'planning': {'pack': {'system_prompt': '旧材料'}}})
    with Session(r.engine, expire_on_commit=False) as session:
        run = GenerationRun(**{**defaults, **values})
        if with_history:
            session.add_all(verified_history(run))
        session.add(run)
        if with_submission:
            chapter = session.get(Chapter, run.chapter_id)
            volume = session.get(Volume, chapter.volume_id)
            intent = run_submission_intent(request_id=run.id, project_id=run.project_id,
                chapter_id=run.chapter_id, max_rewrite_count=run.max_rewrite_count,
                expected_content_text=chapter.content_text or '')
            session.add(make_run_submission(intent, chapter, volume))
        session.commit()
        return run.id


def state(r, run_id):
    with Session(r.engine) as session:
        return runner._run_snapshot(session.get(GenerationRun, run_id))


def counts(r):
    with Session(r.engine) as session:
        return tuple(session.scalar(sa.select(sa.func.count()).select_from(model))
                     for model in (GenerationRun, CriticReport, ChapterVariant))


async def start(r, **values):
    async with r.client() as client:
        return await client.post('/api/generation-runs/start', json={
            'project_id': str(r.pid), 'chapter_id': str(r.cid), 'max_rewrite_count': 0,
            'request_id': str(uuid4()), 'expected_content_text': '作者保存的原稿。', **values,
        })


async def resume(r, run_id):
    async with r.client() as client:
        return await client.post(f'/api/generation-runs/{run_id}/resume')


@pytest.mark.asyncio
async def test_busy_start_creates_no_run_or_model_work(book):
    book.held.add(book.pid)
    response = await start(book)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_busy'
    assert counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()
    book.drafting.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_resume_does_not_change_the_saved_checkpoint_or_budget(book):
    rid = saved_run(book, phase='critic', rewrite_count=2, max_rewrite_count=2,
                    checkpoint_data={'drafting': {'text': DRAFT}})
    before = state(book, rid)
    book.held.add(book.pid)
    response = await resume(book, rid)
    assert response.status_code == 409
    assert state(book, rid) == before and counts(book) == (1, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['missing_chapter', 'null_chapter', 'foreign_chapter', 'trash', 'absent_chapter'])
async def test_invalid_target_is_rejected_before_creation(book, mode):
    values = {}
    if mode == 'foreign_chapter': values['project_id'] = str(book.other)
    elif mode == 'absent_chapter': values['chapter_id'] = str(uuid4())
    elif mode == 'trash':
        with book.engine.begin() as connection:
            connection.execute(sa.update(Project).where(Project.id == book.pid).values(deleted_at=datetime.now(timezone.utc)))
    if mode in {'missing_chapter', 'null_chapter'}:
        body = {'project_id': str(book.pid)}
        if mode == 'null_chapter': body['chapter_id'] = None
        async with book.client() as client:
            response = await client.post('/api/generation-runs/start', json=body)
        assert response.status_code == 422 and book.lock_entries == []
    else:
        response = await start(book, **values)
        assert response.status_code == 404
    assert counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('pipeline_state', ['queued', 'generating', 'pausing'])
async def test_active_book_pipeline_blocks_legacy_creation(book, pipeline_state):
    with Session(book.engine) as session:
        session.add(PipelineRun(project_id=book.pid, state=pipeline_state))
        session.commit()
    response = await start(book)
    assert response.status_code == 409 and counts(book) == (0, 0, 0)
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['start', 'resume'])
async def test_overlapping_requests_generate_once_and_return_saved_state(book, monkeypatch, entry):
    entered, release = asyncio.Event(), asyncio.Event()
    async def draft(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    book.drafting.side_effect = draft
    rid = saved_run(book) if entry == 'resume' else None
    call = (lambda: resume(book, rid)) if rid else (lambda: start(book))
    first = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = await call()
        assert second.status_code == 409
        assert counts(book) == (1, 0, 0)
        release.set()
        response = await first
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['status'] == 'done' and body['editorial_status'] == 'needs_author_review'
        assert body['checkpoint_data']['drafting']['text'] == DRAFT
        assert counts(book) == (1, 1, 0)
        book.drafting.assert_awaited_once()
        assert not book.held
        with Session(book.engine) as session:
            assert session.get(Chapter, book.cid).content_text == '作者保存的原稿。'
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_resume_reloads_a_newer_completed_checkpoint_after_acquiring_lock(book):
    rid = saved_run(book)
    async def finish_elsewhere():
        with Session(book.engine) as session:
            run = session.get(GenerationRun, rid)
            run.status, run.phase = 'done', 'done'
            run.rewrite_count = 2
            run.checkpoint_data = {'drafting': {'text': '另一任务已保存的新稿。'},
                                   'finalize': {'final_text': '另一任务已保存的新稿。'}}
            session.commit()
    book.before_lock = finish_elsewhere
    response = await resume(book, rid)
    assert response.status_code == 200
    assert response.json()['rewrite_count'] == 2
    assert response.json()['checkpoint_data']['finalize']['final_text'] == '另一任务已保存的新稿。'
    book.drafting.assert_not_awaited()
    book.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_read_is_available_while_project_is_busy_or_in_trash(book):
    rid = saved_run(book, status='done', phase='done', checkpoint_data={'drafting': {'text': DRAFT}})
    book.held.add(book.pid)
    with book.engine.begin() as connection:
        connection.execute(sa.update(Project).where(Project.id == book.pid).values(deleted_at=datetime.now(timezone.utc)))
    response = await resume(book, rid)
    assert response.status_code == 200 and response.json()['status'] == 'done'
    assert book.lock_entries == []
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_rejects_legacy_cross_project_target_and_keeps_its_draft(book):
    rid = saved_run(book, project_id=book.other, checkpoint_data={'drafting': {'text': DRAFT}})
    before = state(book, rid)
    response = await resume(book, rid)
    assert response.status_code == 404 and state(book, rid) == before
    book.planning.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_run_returns_404_without_acquiring_execution(book):
    response = await resume(book, uuid4())
    assert response.status_code == 404 and book.lock_entries == []


@pytest.mark.asyncio
async def test_lost_execution_returns_503_with_run_identity_and_preserves_checkpoint(book):
    rid = saved_run(book, phase='critic', checkpoint_data={'drafting': {'text': DRAFT}})
    before = state(book, rid)
    book.review.side_effect = ownership.GenerationExecutionLost()
    response = await resume(book, rid)
    assert response.status_code == 503
    assert response.json()['detail']['run_id'] == str(rid)
    after = state(book, rid)
    assert after['checkpoint_data']['drafting'] == before['checkpoint_data']['drafting']
    assert after['rewrite_count'] == before['rewrite_count']
    recorded = after['checkpoint_data']['attempt_journal']['attempts']
    assert recorded[:-1] == before['checkpoint_data']['attempt_journal']['attempts']
    assert recorded[-1]['kind'] == 'critic' and recorded[-1]['status'] == 'started'
    assert 'failure' not in after['checkpoint_data'] and counts(book) == (1, 0, 0)
    assert not any(call.kwargs['event'] == 'run.failed' for call in runner.run_bus.publish.await_args_list)


@pytest.mark.asyncio
async def test_start_returns_the_known_run_id_when_execution_is_unconfirmed(book):
    book.drafting.side_effect = ownership.GenerationExecutionUnavailable()
    response = await start(book)
    assert response.status_code == 503
    rid = UUID(response.json()['detail']['run_id'])
    assert state(book, rid)['phase'] == 'drafting'
    assert state(book, rid)['status'] == 'running'
    assert counts(book) == (1, 0, 0)


@pytest.mark.asyncio
async def test_cancelled_review_can_resume_the_committed_draft_without_regeneration(book):
    entered = asyncio.Event()
    async def review(**_kwargs):
        entered.set()
        await asyncio.Event().wait()
    book.review.side_effect = review
    pending = asyncio.create_task(start(book))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with Session(book.engine) as reader:
            rid = reader.scalar(sa.select(GenerationRun.id))
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
        assert not book.held
        assert state(book, rid)['checkpoint_data']['drafting']['text'] == DRAFT
        book.review.side_effect = None
        book.review.return_value = make_critic_report(DRAFT, issues=[], stages=[{'name': 'critic', 'status': 'complete'}])
        response = await resume(book, rid)
        assert response.status_code == 200 and response.json()['status'] == 'done'
        book.drafting.assert_awaited_once()
        assert counts(book) == (1, 1, 0)
    finally:
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_resumed_run_keeps_its_exhausted_rewrite_budget(book):
    rid = saved_run(book, status='needs_review', phase='awaiting_review', rewrite_count=1, max_rewrite_count=1,
                    checkpoint_data={'drafting': {'text': DRAFT, 'delivery_status': 'complete'},
                                     'awaiting_review': {'reason': 'review_incomplete'}})
    book.review.side_effect = None
    book.review.return_value = make_critic_report(DRAFT, issues=[{
        'severity': 'hard', 'category': 'resource', 'desc': '交付金额需与前文核对。', 'location': '两千元',
    }], stages=[{'name': 'critic', 'status': 'complete'}])
    response = await resume(book, rid)
    assert response.status_code == 200
    assert response.json()['status'] == 'needs_review'
    assert response.json()['rewrite_count'] == response.json()['max_rewrite_count'] == 1
    assert state(book, rid)['checkpoint_data']['drafting']['text'] == DRAFT
    book.drafting.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_failure_cannot_downgrade_committed_completion(book):
    async def publish(_rid, **kwargs):
        if kwargs['event'] == 'run.done':
            raise RuntimeError('notification failure')
    runner.run_bus.publish.side_effect = publish
    response = await start(book)
    assert response.status_code == 200, response.text
    rid = UUID(response.json()['id'])
    assert state(book, rid)['status'] == 'done' and counts(book) == (1, 1, 0)


@pytest.mark.parametrize('operation', ['execute', 'driver_sql', 'orm_flush', 'commit'])
def test_lost_physical_connection_cannot_overwrite_another_actor(tmp_path, operation):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "physical.sqlite"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, body TEXT)')
        connection.exec_driver_sql("INSERT INTO checkpoints VALUES (1, 'old')")
    mapping = registry()
    class Checkpoint: pass
    mapping.map_imperatively(Checkpoint, sa.Table('checkpoints', mapping.metadata, autoload_with=engine))
    with engine.connect() as old:
        guard = ownership.PhysicalConnectionGuard(old)
        old.execute(sa.text('SELECT 1'))
        old.commit()
        session = Session(old, expire_on_commit=False)
        if operation == 'orm_flush':
            row = session.get(Checkpoint, 1)
            session.commit()
        old.invalidate()
        old.rollback()
        with engine.begin() as current:
            current.exec_driver_sql("UPDATE checkpoints SET body='new actor' WHERE id=1")
        try:
            with pytest.raises(ownership.GenerationExecutionLost):
                if operation == 'execute':
                    old.execute(sa.text("UPDATE checkpoints SET body='stale' WHERE id=1"))
                elif operation == 'driver_sql':
                    old.exec_driver_sql("UPDATE checkpoints SET body='stale' WHERE id=1")
                elif operation == 'orm_flush':
                    row.body = 'stale'
                    session.flush()
                else:
                    # Commit hooks are exercised by an explicit transaction;
                    # the begin hook rejects the reconnect first.
                    old.begin()
                    old.commit()
            assert guard.revoked
        finally:
            session.close()
            old.invalidate()
            guard.detach()
    with engine.begin() as current:
        assert current.scalar(sa.text('SELECT body FROM checkpoints WHERE id=1')) == 'new actor'
        current.exec_driver_sql("UPDATE checkpoints SET body='later normal writer' WHERE id=1")
    mapping.dispose()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['before_commit', 'after_commit', 'cancel'])
async def test_unconfirmed_commit_permanently_revokes_the_session(tmp_path, monkeypatch, failure):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "commit.sqlite"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, body TEXT)')
        connection.exec_driver_sql("INSERT INTO checkpoints VALUES (1, 'previous')")
    with engine.connect() as connection:
        guard = ownership.PhysicalConnectionGuard(connection)
        connection.exec_driver_sql("UPDATE checkpoints SET body='submitted' WHERE id=1")
        async def commit(_session):
            if failure == 'after_commit': connection.commit()
            if failure == 'cancel': raise asyncio.CancelledError()
            raise RuntimeError('private acknowledgement details')
        monkeypatch.setattr(AsyncSession, 'commit', commit)
        session = ownership.GenerationExecutionSession(guard=guard, expire_on_commit=False)
        try:
            expected = asyncio.CancelledError if failure == 'cancel' else ownership.GenerationExecutionUnavailable
            with pytest.raises(expected) as caught:
                await session.commit()
            assert 'private' not in str(caught.value) and guard.revoked
            connection.rollback()
            with pytest.raises(ownership.GenerationExecutionLost):
                connection.exec_driver_sql("UPDATE checkpoints SET body='failed' WHERE id=1")
        finally:
            connection.invalidate()
            guard.detach()
            await session.close()
    with engine.connect() as reader:
        expected_body = 'submitted' if failure == 'after_commit' else 'previous'
        assert reader.scalar(sa.text('SELECT body FROM checkpoints WHERE id=1')) == expected_body
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'busy', 'acquire_error', 'commit_error',
                                    'raw_acquire_error', 'raw_commit_error', 'cancel_acquire', 'cancel_body', 'body_error'])
async def test_execution_discards_connection_on_every_exit(tmp_path, monkeypatch, outcome):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "lifecycle.sqlite"}')
    record = NS(invalidated=0, closed=0, yielded=0)

    class Connection:
        dialect = NS(name='postgresql')
        def __init__(self, sync): self.sync_connection = sync
        async def scalar(self, statement, params):
            record.key = params['key']
            if outcome == 'acquire_error': raise sa.exc.OperationalError('lock', {}, Exception('private db detail'))
            if outcome == 'raw_acquire_error': raise TimeoutError('private db detail')
            if outcome == 'cancel_acquire': raise asyncio.CancelledError()
            return outcome != 'busy'
        async def commit(self):
            if outcome == 'commit_error': raise sa.exc.OperationalError('commit', {}, Exception('private db detail'))
            if outcome == 'raw_commit_error': raise RuntimeError('private commit detail')
            self.sync_connection.commit()
        async def invalidate(self):
            record.invalidated += 1
            self.sync_connection.invalidate()

    @asynccontextmanager
    async def connect():
        with engine.connect() as sync:
            try: yield Connection(sync)
            finally: record.closed += 1

    class OwnedSession:
        def __init__(self, **kwargs):
            record.yielded += 1
            assert kwargs['bind'].sync_connection is kwargs['guard'].connection
        async def close(self): pass

    monkeypatch.setattr(ownership, '_execution_engine', lambda _db: NS(connect=connect))
    monkeypatch.setattr(ownership, 'GenerationExecutionSession', OwnedSession)
    pid = uuid4()
    async def run():
        async with ownership.generation_execution_session(NS(), pid):
            if outcome == 'cancel_body': raise asyncio.CancelledError()
            if outcome == 'body_error': raise ValueError('model failed')
    if outcome == 'success':
        await run()
    else:
        expected = (PipelineConflict if outcome == 'busy' else
                    asyncio.CancelledError if outcome.startswith('cancel') else
                    ValueError if outcome == 'body_error' else ownership.GenerationExecutionUnavailable)
        with pytest.raises(expected): await run()
    assert record.invalidated == record.closed == 1
    assert record.yielded == int(outcome in {'success', 'cancel_body', 'body_error'})
    assert record.key == ownership.project_lock_key(pid)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['session', 'connection'])
async def test_repeated_cancellation_drains_cleanup_before_connection_exit(tmp_path, monkeypatch, stage):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "cancellation.sqlite"}')
    body_done, cleanup_entered, cleanup_release, cleanup_done = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )
    record = NS(session_closed=0, invalidated=0, connection_exited=0, cleanup_cancelled=False)
    real_session = ownership.GenerationExecutionSession

    async def delayed_cleanup():
        cleanup_entered.set()
        try:
            await cleanup_release.wait()
        except asyncio.CancelledError:
            record.cleanup_cancelled = True
            raise
        finally:
            cleanup_done.set()

    class Connection:
        dialect = NS(name='postgresql')
        def __init__(self, sync): self.sync_connection = sync
        async def scalar(self, *_args): return True
        async def commit(self): self.sync_connection.commit()
        async def invalidate(self):
            if stage == 'connection': await delayed_cleanup()
            self.sync_connection.invalidate()
            record.invalidated += 1

    @asynccontextmanager
    async def connect():
        with engine.connect() as sync:
            try: yield Connection(sync)
            finally: record.connection_exited += 1

    def session_factory(**kwargs):
        # Retain SQLAlchemy's real async __aexit__, including its shielded
        # close, so this covers interruption of an in-flight cleanup task.
        session = real_session(guard=kwargs['guard'], expire_on_commit=False)
        close = session.close
        async def close_later():
            if stage == 'session': await delayed_cleanup()
            await close()
            record.session_closed += 1
        session.close = close_later
        return session

    monkeypatch.setattr(ownership, '_execution_engine', lambda _db: NS(connect=connect))
    monkeypatch.setattr(ownership, 'GenerationExecutionSession', session_factory)
    async def run():
        async with ownership.generation_execution_session(NS(), uuid4()):
            body_done.set()
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(body_done.wait(), 2)
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), 'The owner must wait until cleanup has finished.'
        assert record.connection_exited == 0
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert record.session_closed == record.invalidated == record.connection_exited == 1
        assert not record.cleanup_cancelled
    finally:
        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(cleanup_done.wait(), 2)
        engine.dispose()
