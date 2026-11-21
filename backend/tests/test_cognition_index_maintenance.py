"""Only permanent project absence permits asynchronous derived-index cleanup."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from qdrant_client.models import Distance, VectorParams
from sqlalchemy import update

from app.models.project import Project
from app.services.cognition_index_maintenance import cleanup_deleted_projects, indexed_project
from app.services.qdrant_store import cognition_collection
from tests.test_cognition_retrieval import FixedSpace, vector_db
from tests.test_character_state_context import state_db


@pytest.mark.parametrize('name', ['chapter_summaries', 'chapter_cognition', 'chapter_cognition__x',
    'chapter_cognition__' + 'a' * 32 + '__' + 'b' * 64 + '__0',
    'other__' + 'a' * 32 + '__' + 'b' * 64 + '__2'])
def test_unrecognized_collection_is_never_deletion_authority(name):
    assert indexed_project(name) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('trash', [False, True])
async def test_permanent_absence_cleans_late_writes_but_keeps_live_and_trash(state_db, vector_db, monkeypatch, trash):
    r = state_db
    async def scalar(statement):
        return (await r.db.execute(statement)).scalar_one_or_none()
    monkeypatch.setattr(r.db, 'scalar', scalar, raising=False)
    if trash:
        r.session.execute(update(Project).values(deleted_at=datetime.now(timezone.utc))); r.session.commit()
    opened = False
    @asynccontextmanager
    async def sessions():
        nonlocal opened
        opened = True
        try: yield r.db
        finally: r.session.rollback(); opened = False
    existing = cognition_collection(r.pid, FixedSpace.identity, 2)
    deleted = cognition_collection(uuid4(), FixedSpace.identity, 2)
    unrelated = 'chapter_chunks__unrelated'
    for name in (existing, deleted, unrelated):
        await vector_db.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    remove = vector_db.delete_collection
    async def outside_transaction(**kwargs):
        assert not opened
        return await remove(**kwargs)
    monkeypatch.setattr(vector_db, 'delete_collection', outside_transaction)
    assert await cleanup_deleted_projects(sessions, client=vector_db) == {'deleted': 1, 'examined': 2, 'busy': 0}
    assert {c.name for c in (await vector_db.get_collections()).collections} == {existing, unrelated}
    # A writer started before deletion may finish later. Periodic cleanup
    # must remove the recreated orphan on its next scan as well.
    await vector_db.create_collection(deleted, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    assert (await cleanup_deleted_projects(sessions, client=vector_db))['deleted'] == 1


@pytest.mark.asyncio
async def test_database_failure_does_not_mean_project_absence(vector_db, monkeypatch):
    name = cognition_collection(uuid4(), FixedSpace.identity, 2)
    await vector_db.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    @asynccontextmanager
    async def sessions(): yield NS(execute=AsyncMock(side_effect=RuntimeError('unavailable')))
    remove = AsyncMock()
    monkeypatch.setattr(vector_db, 'delete_collection', remove)
    with pytest.raises(RuntimeError): await cleanup_deleted_projects(sessions, client=vector_db)
    remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['busy', 'project_returned', 'lost_lock'])
async def test_orphan_cleanup_requires_index_lock_and_fresh_absence(vector_db, monkeypatch, state):
    pid = uuid4()
    name = cognition_collection(pid, FixedSpace.identity, 2)
    await vector_db.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    result = NS(scalars=lambda: NS(all=lambda: []))
    @asynccontextmanager
    async def sessions():
        yield NS(execute=AsyncMock(return_value=result), scalar=AsyncMock(
            return_value=pid if state == 'project_returned' else None))
    @asynccontextmanager
    async def lock(project_id, *, wait):
        assert project_id == str(pid) and not wait
        yield False if state == 'busy' else NS(check=AsyncMock(
            side_effect=RuntimeError('lost') if state == 'lost_lock' else None))
    remove = AsyncMock()
    monkeypatch.setattr(vector_db, 'delete_collection', remove)
    if state == 'lost_lock':
        with pytest.raises(RuntimeError):
            await cleanup_deleted_projects(sessions, client=vector_db, lock_factory=lock)
    else:
        result = await cleanup_deleted_projects(sessions, client=vector_db, lock_factory=lock)
        assert result['deleted'] == 0 and result['busy'] == int(state == 'busy')
    remove.assert_not_awaited()
