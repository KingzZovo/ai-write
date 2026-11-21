"""Actual durable dispatch state without a broker or service connection."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.models.generation_batch import GenerationBatch
from app.models.project import Project
from app.services.batch_dispatch import dispatch_batches
from app.tasks import celery_app
from tests.test_batch_generation_submissions import batch_worker
from tests.test_generation_submissions import book, queued, worker


def add_batch(r, *, status='pending', version=0, heartbeat=None, dispatched=None):
    row = GenerationBatch(id=uuid4(), project_id=r.pid, request_hash='a' * 64, status=status,
        total_chapters=1005, execution_version=version, heartbeat_at=heartbeat, dispatched_at=dispatched,
        worker_id=uuid4() if status == 'running' else None)
    r.session.add(row)
    r.session.commit()
    return row.id


@pytest.mark.asyncio
async def test_dispatch_is_bounded_reads_no_manuscripts_and_retries_the_same_committed_request(batch_worker):
    r = batch_worker
    now = datetime.now(timezone.utc)
    ids = [add_batch(r) for _ in range(4)]
    delivered, statements = [], []
    async def publish(pid, rid):
        assert pid == str(r.pid)
        async with r.sessions() as reader:
            row = await reader.get(GenerationBatch, rid)
            assert row.dispatched_at is not None and row.status == 'pending'
        delivered.append(rid)
    def capture(_conn, _cursor, statement, _params, _context, _many): statements.append(statement)
    sa.event.listen(r.engine, 'before_cursor_execute', capture)
    try:
        assert await dispatch_batches(r.sessions, publish, limit=2, now=now) == 2
        assert await dispatch_batches(r.sessions, publish, limit=2, now=now) == 2
        assert await dispatch_batches(r.sessions, publish, limit=2, now=now) == 0
        assert set(delivered) == {str(rid) for rid in ids}
        assert await dispatch_batches(r.sessions, publish, limit=2, now=now + timedelta(seconds=31)) == 2
    finally:
        sa.event.remove(r.engine, 'before_cursor_execute', capture)
    assert all('content_text' not in sql and 'result_text' not in sql and 'outline_json' not in sql for sql in statements)
    assert len(set(delivered)) == 4
    r.implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_selects_only_pending_or_stale_proven_executions(batch_worker):
    r = batch_worker
    now = datetime.now(timezone.utc)
    pending = add_batch(r)
    stale = add_batch(r, status='running', version=1, heartbeat=now - timedelta(seconds=120))
    add_batch(r, status='running', version=0, heartbeat=now - timedelta(seconds=120))
    add_batch(r, status='running', version=1)
    add_batch(r, status='running', version=1, heartbeat=now)
    for status in ('completed', 'interrupted', 'error', 'needs_review'):
        add_batch(r, status=status, version=1, heartbeat=now - timedelta(seconds=120))
    calls = []
    async def publish(pid, rid): calls.append(rid)
    assert await dispatch_batches(r.sessions, publish, now=now) == 2
    assert set(calls) == {str(pending), str(stale)}
    (r.session.get(Project, r.pid)).deleted_at = now
    r.session.commit()
    assert await dispatch_batches(r.sessions, publish, now=now + timedelta(seconds=120)) == 0


@pytest.mark.asyncio
async def test_lost_publish_response_does_not_overwrite_new_worker_state_or_starve_other_batches(batch_worker):
    r = batch_worker
    now = datetime.now(timezone.utc)
    first, second = add_batch(r), add_batch(r)
    calls = []
    async def publish(pid, rid):
        calls.append(rid)
        if len(calls) == 1:
            async with r.sessions() as db:
                row = await db.get(GenerationBatch, rid)
                row.status, row.error_message = 'needs_review', '作者需要检查已生成稿。'
                await db.commit()
            raise RuntimeError('test lost broker reply')
    assert await dispatch_batches(r.sessions, publish, limit=1, now=now) == 0
    assert await dispatch_batches(r.sessions, publish, limit=1, now=now) == 1
    assert set(calls) == {str(first), str(second)}
    r.session.expire_all()
    changed = r.session.get(GenerationBatch, UUID(calls[0]))
    assert changed.status == 'needs_review' and changed.error_message == '作者需要检查已生成稿。'


def test_periodic_scan_uses_control_queue_and_the_runner_has_no_unbounded_broker_retry():
    from app.tasks.generation_tasks import run_generation_batch, dispatch_generation_batches
    schedule = celery_app.conf.beat_schedule['generation-batch-recovery']
    assert schedule['task'] == dispatch_generation_batches.name and schedule['schedule'] == 30.0
    assert celery_app.conf.task_routes[dispatch_generation_batches.name]['queue'] == 'pipeline-control'
    assert run_generation_batch.name == 'tasks.run_generation_batch'
    assert run_generation_batch.acks_late is False
