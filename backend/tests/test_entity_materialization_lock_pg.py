"""Real PostgreSQL lock lifetime and snapshot ordering gates."""
import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.models.project import Project
from app.services.entity_materialization_lock import require_materialization_lock, MaterializationBusy
from tests.test_pipeline_control import runtime_db


@pytest.fixture(autouse=True)
def no_graph_cleanup(monkeypatch):
    monkeypatch.setattr('app.services.character_deletion.stage_character_deletions', AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_materialization_lock_is_project_scoped_and_released_on_rollback(runtime_db):
    r = runtime_db
    async with r.sessions() as first, r.sessions() as second, r.sessions() as other:
        await require_materialization_lock(first, r.project)
        with pytest.raises(MaterializationBusy): await require_materialization_lock(second, r.project)
        await require_materialization_lock(other, uuid4())
        await first.rollback()
        await require_materialization_lock(second, r.project)


@pytest.mark.asyncio
async def test_public_materializer_takes_fresh_snapshot_after_previous_commit(runtime_db, monkeypatch):
    from app.tasks import entity_tasks
    r = runtime_db
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    graph = {'title': '旧档案'}
    reads = []
    async def snapshot(db, **kwargs):
        title = graph['title']
        reads.append(title)
        if title == '旧档案':
            entered.set()
            await release.wait()
        (await db.get(Project, r.project)).title = title
        return {'chars_seen': 1}
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', snapshot)
    async def materialize():
        return await entity_tasks._materialize_entities_to_postgres(project_id=str(r.project),
            chapter_idx=0, caller='test', strict=True, graph_driver=object())
    task = asyncio.create_task(materialize())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        graph['title'] = '新档案'
        with pytest.raises(MaterializationBusy): await materialize()
        assert reads == ['旧档案']  # Busy caller must not capture a snapshot.
        release.set()
        await asyncio.wait_for(task, 3)
        await materialize()
        async with r.sessions() as db:
            assert (await db.get(Project, r.project)).title == '新档案'
        assert reads == ['旧档案', '新档案']
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_materializer_releases_transaction_lock(runtime_db, monkeypatch):
    from app.tasks import entity_tasks
    r = runtime_db
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    entered = asyncio.Event()
    async def snapshot(db, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(entity_tasks, '_materialize_entity_snapshot', snapshot)
    task = asyncio.create_task(entity_tasks._materialize_entities_to_postgres(project_id=str(r.project),
        chapter_idx=0, caller='test', strict=True, graph_driver=object()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with r.sessions() as db:
        await require_materialization_lock(db, r.project)
