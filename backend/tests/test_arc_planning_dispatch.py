"""Dispatch only original IDs after a confirmed marker; preserve call budgets."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.project import Project
from app.services.arc_planning_dispatch import dispatch_arc_requests
from app.services.arc_planning_requests import prepare_arc_request
from app.services.arc_planning_sources import read_planning_source
from app.services.arc_planning_contract import source_token
from tests.support.arc_planning_runtime import planning_book, snapshot
from tests.test_generation_execution import LocalSession


async def pending(r):
    pid, rid = uuid4(), uuid4()
    with r.engine.begin() as db:
        db.execute(sa.insert(Project).values(id=pid, title="调度试书", target_word_count=5_000_000))
    async with r.sessions() as db:
        source = await read_planning_source(db, pid)
        row, created = await prepare_arc_request(db, pid, dict(request_id=str(rid), expected_source=source_token(pid, source),
            mode="start", idea="原请求", background="", core_setup="", opening_scene="", target_chapters=4))
        assert created
        await db.commit()
    return row


@pytest.mark.asyncio
async def test_bounded_dispatch_reads_metadata_and_does_not_starve_other_projects(planning_book):
    r = planning_book; rows = [await pending(r) for _ in range(3)]
    now = datetime.now(timezone.utc); delivered = []
    async def publish(identity):
        assert snapshot(r, identity)["dispatched_at"] is not None
        delivered.append(identity)
    r.queries.clear()
    for _ in range(3): assert await dispatch_arc_requests(r.sessions, publish, limit=1, now=now) == 1
    assert set(delivered) == {str(row.id) for row in rows}
    # Snapshot assertions deliberately read full rows; dispatcher queries do not.
    query_reads = [query for query in r.queries if query.startswith('SELECT') and 'LIMIT' in query]
    assert all('journal_json' not in query and 'source_json' not in query for query in query_reads)
    assert await dispatch_arc_requests(r.sessions, publish, limit=1, now=now) == 0
    assert await dispatch_arc_requests(r.sessions, publish, limit=1, now=now + timedelta(seconds=31)) == 1
    assert all(snapshot(r, row.id)["execution_attempts"] == 0 for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before", "after"])
async def test_unconfirmed_dispatch_marker_does_not_publish_or_reset_the_request(planning_book, fault):
    r = planning_book; row = await pending(r)
    class Unconfirmed(LocalSession):
        async def commit(self):
            if fault == "after": await super().commit()
            raise RuntimeError("synthetic lost marker response")
    @asynccontextmanager
    async def sessions():
        with Session(r.engine) as db: yield Unconfirmed(db)
    calls = []
    async def publish(identity): calls.append(identity)
    assert await dispatch_arc_requests(sessions, publish) == 0 and not calls
    state = snapshot(r, row.id)
    assert (state["dispatched_at"] is not None) == (fault == "after")
    assert state["execution_attempts"] == 0 and state["status"] == "pending"


@pytest.mark.asyncio
async def test_broker_failure_keeps_original_number_and_budget(planning_book):
    r = planning_book; row = await pending(r); calls = []
    async def publish(identity):
        calls.append(identity)
        raise RuntimeError("synthetic broker failure")
    now = datetime.now(timezone.utc)
    assert await dispatch_arc_requests(r.sessions, publish, now=now) == 0
    assert await dispatch_arc_requests(r.sessions, publish, now=now + timedelta(seconds=31)) == 0
    assert calls == [str(row.id), str(row.id)]
    assert snapshot(r, row.id)["execution_attempts"] == 0


@pytest.mark.asyncio
async def test_only_pending_or_stale_running_requests_are_redelivered(planning_book):
    r = planning_book; now = datetime.now(timezone.utc)
    eligible = await pending(r)
    stale = await pending(r)
    with r.engine.begin() as db:
        db.execute(sa.update(Request).where(Request.id == stale.id).values(status="running", execution_id=uuid4(), heartbeat_at=now - timedelta(seconds=301)))
    for change in ({"status": "cancelled"}, {"status": "completed"}, {"status": "needs_review"},
                   {"status": "running", "execution_id": uuid4(), "heartbeat_at": now},
                   {"status": "running", "execution_id": None, "heartbeat_at": now - timedelta(seconds=301)}):
        row = await pending(r)
        with r.engine.begin() as db: db.execute(sa.update(Request).where(Request.id == row.id).values(**change))
    removed = await pending(r)
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == removed.project_id).values(deleted_at=now))
    delivered = []
    async def publish(identity): delivered.append(identity)
    assert await dispatch_arc_requests(r.sessions, publish, now=now) == 2
    assert set(delivered) == {str(eligible.id), str(stale.id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "delete"])
async def test_dispatch_rechecks_author_changes_after_discovery(planning_book, change):
    r = planning_book; row = await pending(r); entered = 0
    @asynccontextmanager
    async def sessions():
        nonlocal entered
        entered += 1
        if entered == 2:
            with r.engine.begin() as db:
                if change == "cancel": db.execute(sa.update(Request).where(Request.id == row.id).values(status="cancelled"))
                else: db.execute(sa.delete(Project).where(Project.id == row.project_id))
        async with r.sessions() as db: yield db
    calls = []
    async def publish(identity): calls.append(identity)
    assert await dispatch_arc_requests(sessions, publish) == 0 and not calls


def test_arc_worker_and_recovery_are_registered_separately_from_generation_types():
    from app.tasks import celery_app
    from app.tasks.arc_tasks import run_arc_planning, dispatch_arc_planning
    from app.services.generation_task_dispatch import TASK_TYPES
    assert celery_app.tasks['tasks.run_arc_planning'].run == run_arc_planning.run
    assert celery_app.tasks['tasks.dispatch_arc_planning'].run == dispatch_arc_planning.run
    assert celery_app.conf.task_routes['tasks.dispatch_arc_planning']['queue'] == 'pipeline-control'
    assert celery_app.conf.beat_schedule['arc-planning-recovery']['task'] == 'tasks.dispatch_arc_planning'
    assert TASK_TYPES == ('chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference')
