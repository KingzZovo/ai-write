"""Durable task dispatch state with real transactions and no broker."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.project import Project
from app.services.generation_task_dispatch import dispatch_tasks
from app.tasks import celery_app, generation_tasks
from tests.test_generation_execution import LocalSession
from tests.test_generation_task_execution import snapshot, submit, task_book


@pytest.mark.asyncio
async def test_bounded_scan_reads_no_prose_and_publishes_only_after_confirmed_marker(task_book):
    r = task_book
    now = datetime.now(timezone.utc)
    identities = [submit(r) for _ in range(5)]
    original = {key: snapshot(r, key) for key in identities}
    delivered, statements = [], []
    async def publish(key): delivered.append(key)
    def capture(_c, _u, statement, _p, _ctx, _many): statements.append(statement)
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        assert await dispatch_tasks(r.sessions, publish, limit=2, now=now) == 2
        assert await dispatch_tasks(r.sessions, publish, limit=2, now=now) == 2
        assert await dispatch_tasks(r.sessions, publish, limit=2, now=now) == 1
        assert await dispatch_tasks(r.sessions, publish, limit=2, now=now) == 0
    finally:
        sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert set(delivered) == {str(key) for key in identities}
    assert all(not any(name in sql for name in ('content_text', 'result_text', 'progress_text', 'execution_params_json'))
        for sql in statements)
    for key in identities:
        state = snapshot(r, key)
        assert state['dispatched_at'] is not None
        assert {name: value for name, value in state.items() if name != 'dispatched_at'} == {
            name: value for name, value in original[key].items() if name != 'dispatched_at'}
    assert await dispatch_tasks(r.sessions, publish, limit=2, now=now + timedelta(seconds=31)) == 2
    assert len(set(delivered)) == 5 and not r.calls


@pytest.mark.asyncio
async def test_only_eligible_independent_tasks_are_selected(task_book):
    r = task_book
    now = datetime.now(timezone.utc)
    pending = submit(r)
    stale_ids = []
    for status in ('running', 'evaluating', 'polishing', 'regenerating'):
        identity = submit(r)
        with r.engine.begin() as db:
            db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(status=status,
                execution_version=1, execution_id=uuid4(), heartbeat_at=now - timedelta(seconds=120)))
        stale_ids.append(identity)
    excluded = [
        {'status': 'running', 'execution_version': 0},
        {'status': 'running', 'heartbeat_at': now},
        {'status': 'running', 'heartbeat_at': None},
        {'status': 'running', 'execution_id': None},
        *({'status': state} for state in ('completed', 'failed', 'needs_review', 'needs_repair', 'cancelled')),
        {'params_json': {'batch_request_id': str(uuid4())}},
        {'params_json': {'pipeline_id': str(uuid4())}},
        {'params_json': {'cancelled_at': '2026-10-09'}},
        {'task_type': 'unknown'}, {'dispatched_at': now},
    ]
    for values in excluded:
        identity = submit(r)
        with r.engine.begin() as db:
            db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(**{
                'execution_version': 1, 'execution_id': uuid4(), 'heartbeat_at': now - timedelta(seconds=120), **values}))
    delivered = []
    async def publish(key): delivered.append(key)
    assert await dispatch_tasks(r.sessions, publish, now=now) == 5
    assert set(delivered) == {str(key) for key in [pending, *stale_ids]}
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == r.pid).values(deleted_at=now))
    assert await dispatch_tasks(r.sessions, publish, now=now + timedelta(seconds=120)) == 0


@pytest.mark.asyncio
async def test_lost_broker_reply_keeps_the_id_and_does_not_starve_other_tasks(task_book):
    r = task_book
    now = datetime.now(timezone.utc)
    identities = [submit(r) for _ in range(3)]
    delivered = []
    async def publish(key):
        delivered.append(key)
        # The marker must already be visible on an independent connection.
        assert snapshot(r, UUID(key))['dispatched_at'] is not None
        raise RuntimeError('offline lost broker response')
    for _ in range(3): assert await dispatch_tasks(r.sessions, publish, limit=1, now=now) == 0
    assert set(delivered) == {str(key) for key in identities}
    assert await dispatch_tasks(r.sessions, publish, limit=1, now=now) == 0
    assert len(delivered) == 3
    assert await dispatch_tasks(r.sessions, publish, limit=1, now=now + timedelta(seconds=31)) == 0
    assert len(delivered) == 4 and all(snapshot(r, key)['attempt_count'] == 0 for key in identities)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', ['before', 'after'])
async def test_marker_commit_uncertainty_does_not_publish_or_invent_a_new_request(task_book, ack):
    r = task_book
    identity = submit(r)
    now = datetime.now(timezone.utc)
    class Uncertain(LocalSession):
        async def commit(self):
            if ack == 'after': await super().commit()
            raise RuntimeError('offline ambiguous marker commit')
    @asynccontextmanager
    async def uncertain():
        with Session(r.engine, expire_on_commit=False) as db: yield Uncertain(db)
    delivered = []
    async def publish(key): delivered.append(key)
    assert await dispatch_tasks(uncertain, publish, now=now) == 0
    assert not delivered
    state = snapshot(r, identity)
    assert (state['dispatched_at'] is not None) == (ack == 'after')
    assert state['attempt_count'] == 0 and state['status'] == 'pending'
    assert await dispatch_tasks(r.sessions, publish, now=now + timedelta(seconds=31)) == 1
    assert delivered == [str(identity)]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel', 'delete', 'adopted_by_batch', 'completed'])
async def test_marker_transaction_rechecks_changes_after_discovery(task_book, change):
    r = task_book
    identity = submit(r)
    selections = 0
    @asynccontextmanager
    async def sessions():
        nonlocal selections
        selections += 1
        if selections == 2:
            with r.engine.begin() as db:
                if change == 'delete': db.execute(sa.update(Project).where(Project.id == r.pid).values(deleted_at=datetime.now(timezone.utc)))
                else:
                    values = {'cancel': {'status': 'cancelled'}, 'completed': {'status': 'completed'},
                        'adopted_by_batch': {'params_json': {'batch_request_id': str(uuid4())}}}[change]
                    db.execute(sa.update(GenerationTask).where(GenerationTask.id == identity).values(**values))
        async with r.sessions() as db: yield db
    delivered = []
    async def publish(key): delivered.append(key)
    assert await dispatch_tasks(sessions, publish) == 0
    assert not delivered and snapshot(r, identity)['dispatched_at'] is None


@pytest.mark.asyncio
async def test_overlapping_scans_publish_each_marker_once(task_book):
    r = task_book
    identity = submit(r)
    now = datetime.now(timezone.utc)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def publish(key):
        calls.append(key)
        entered.set()
        await release.wait()
    first = asyncio.create_task(dispatch_tasks(r.sessions, publish, now=now))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await dispatch_tasks(r.sessions, publish, now=now) == 0
        release.set()
        assert await first == 1 and calls == [str(identity)]
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


def test_beat_uses_control_queue_and_explicit_recovery_task():
    schedule = celery_app.conf.beat_schedule['independent-generation-recovery']
    assert schedule['task'] == generation_tasks.dispatch_generation_tasks.name
    assert schedule['schedule'] == 30.0 and schedule['options']['expires'] == 30
    assert celery_app.conf.task_routes[schedule['task']]['queue'] == 'pipeline-control'
    assert generation_tasks.recover_async_generation.acks_late is False
    assert generation_tasks.run_async_generation.name != generation_tasks.recover_async_generation.name
