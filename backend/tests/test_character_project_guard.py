"""Official mutations hold project protection and release it before projection."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.api import neo4j_settings as api
from app.models.project import Project
from app.services.character_project_guard import character_graph_write_guard
from tests.support.arc_planning_runtime import planning_book
from tests.support.character_project import CharacterProjectDB


async def write(action, db, pid, driver):
    if action == 'profile':
        return await api.upsert_character(pid, api.Neo4jCharacterUpsertRequest(
            name='林禾', profile_json={}, expected_profile_json=None), driver, db)
    if action == 'delete':
        return await api.delete_character(pid, api.Neo4jCharacterDeleteRequest(
            name='林禾', expected_profile_json={}, deletion_token=uuid4()), driver, db)
    if action == 'location':
        return await api.set_character_location(pid, api.Neo4jSetLocationRequest(
            character='林禾', location='船舱', chapter_start=1), driver, db)
    return await api.set_character_membership(pid, api.Neo4jSetMembershipRequest(
        character='林禾', organization='渡口船行', chapter_start=1), driver, db)


def mutation_double(monkeypatch, db, *, failure=None):
    calls = []
    async def graph(*args, **kwargs):
        assert db.held, 'graph write was allowed outside the project guard'
        calls.append((args, kwargs))
        if failure: raise failure
        return 'saved-token'
    class Driver:
        def session(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def run(self, *args, **kwargs):
            await graph(*args, **kwargs)
            return NS(consume=AsyncMock())
    async def project(**kwargs):
        assert not db.held, 'projection could deadlock planning while the guard was held'
        return {}
    monkeypatch.setattr('app.services.character_profile_write.write_character_profile', graph)
    monkeypatch.setattr('app.services.character_deletion.delete_character_graph', graph)
    monkeypatch.setattr('app.services.character_profile_recovery.acknowledge_profiles', AsyncMock())
    materialize = AsyncMock(side_effect=project)
    monkeypatch.setattr(api, '_materialize_entities_to_postgres', materialize)
    return NS(driver=Driver(), calls=calls, materialize=materialize)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['profile', 'delete', 'location', 'membership'])
async def test_official_graph_writes_hold_share_guard_and_release_before_projection(monkeypatch, action):
    pid = str(uuid4()); db = CharacterProjectDB(pid)
    m = mutation_double(monkeypatch, db)
    result = await write(action, db, pid.upper(), m.driver)
    assert result['status'] == 'accepted' and m.calls
    statement = db.scalar.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert 'FOR SHARE' in sql and 'deleted_at IS NULL' in sql
    assert statement.get_execution_options()['autoflush'] is False
    assert not db.held and db.rollback.await_count == 1
    db.commit.assert_not_awaited()
    m.materialize.assert_awaited_once()
    assert m.materialize.call_args.kwargs['project_id'] == pid


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['profile', 'delete', 'location', 'membership'])
@pytest.mark.parametrize('state,expected', [('invalid', 422), ('missing', 404), ('deleted', 404)])
async def test_unavailable_project_never_reaches_graph_or_projection(planning_book, monkeypatch, action, state, expected):
    r = planning_book
    pid = str(r.pid)
    if state == 'invalid': pid = 'not-a-project-id'
    elif state == 'missing': pid = str(uuid4())
    else:
        with r.engine.begin() as connection:
            connection.execute(sa.update(Project).where(Project.id == r.pid).values(deleted_at=datetime.now(timezone.utc)))
    db_double = CharacterProjectDB(r.pid)
    m = mutation_double(monkeypatch, db_double)
    async with r.sessions() as db:
        with pytest.raises(HTTPException) as error:
            await write(action, db, pid, m.driver)
        assert error.value.status_code == expected
        assert not db.session.in_transaction()
    assert not m.calls
    m.materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['profile', 'delete', 'location', 'membership'])
async def test_failed_graph_write_releases_project_and_hides_private_error(monkeypatch, action):
    pid = str(uuid4()); db = CharacterProjectDB(pid)
    m = mutation_double(monkeypatch, db, failure=RuntimeError('private graph diagnostic'))
    with pytest.raises(HTTPException) as error:
        await write(action, db, pid, m.driver)
    assert error.value.status_code == 503 and 'private' not in str(error.value.detail)
    assert not db.held and db.rollback.await_count == 1
    m.materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_failure_does_not_attempt_a_character_write(monkeypatch):
    pid = str(uuid4()); db = CharacterProjectDB(pid)
    db.scalar.side_effect = sa.exc.SQLAlchemyError('private database diagnostic')
    m = mutation_double(monkeypatch, db)
    with pytest.raises(HTTPException) as error:
        await write('profile', db, pid, m.driver)
    assert error.value.status_code == 503 and 'private' not in str(error.value.detail)
    assert not m.calls and db.rollback.await_count == 1


@pytest.mark.asyncio
async def test_repeated_request_cancellation_drains_project_lock_release():
    pid = str(uuid4()); db = CharacterProjectDB(pid)
    entered, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def rollback():
        rolling_back.set()
        await release.wait()
        db.held = False
    db.rollback.side_effect = rollback
    async def writer():
        async with character_graph_write_guard(db, pid):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(rolling_back.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert db.held and not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert not db.held and db.rollback.await_count == 1
