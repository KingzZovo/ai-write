"""Real dual-store gates for replay without deleting a recreated character."""
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, event

from app.models import Base
from app.models.project import Character
from app.models.entity_extraction import AppliedCharacterDeletion
from app.services.character_deletion import delete_character_graph
from app.services.character_profile_write import write_character_profile
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from tests.test_pipeline_control import runtime_db
from tests.test_entity_projection import env


async def prepare(env, monkeypatch):
    r = env.r
    async with r.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    async with r.sessions() as db:
        old = Character(project_id=r.project, name='周砚', profile_json={'identity': '旧人物'})
        db.add(old); await db.commit(); old_id = old.id
    await write_character_profile(env.driver, r.project, '周砚', {'identity': '旧人物'}, None)
    token = uuid4()
    await delete_character_graph(env.driver, r.project, '周砚', {'identity': '旧人物'}, token)
    return r, old_id, token


@pytest.mark.asyncio
async def test_lost_acknowledgement_does_not_delete_recreated_same_name(env, monkeypatch):
    from app.services import character_deletion
    r, old_id, token = await prepare(env, monkeypatch)
    await write_character_profile(env.driver, r.project, '周砚', {'identity': '同名新人物'}, None)
    acknowledge = character_deletion.acknowledge_character_deletions
    monkeypatch.setattr(character_deletion, 'acknowledge_character_deletions', AsyncMock(side_effect=RuntimeError('lost graph ack')))
    with pytest.raises(RuntimeError):
        await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0, caller='test', strict=True, graph_driver=env.driver)
    async with r.sessions() as db:
        new = await db.scalar(select(Character).where(Character.project_id == r.project))
        new_id = new.id
        assert new_id != old_id and new.profile_json == {'identity': '同名新人物'}
        assert await db.get(AppliedCharacterDeletion, (token, r.project)) is not None
    monkeypatch.setattr(character_deletion, 'acknowledge_character_deletions', acknowledge)
    await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0, caller='test', strict=True, graph_driver=env.driver)
    await delete_character_graph(env.driver, r.project, '周砚', {'identity': '旧人物'}, token)
    async with r.sessions() as db:
        assert (await db.scalar(select(Character).where(Character.project_id == r.project))).id == new_id
    async with env.driver.session() as graph:
        row = await (await graph.run('MATCH (c:Character {project_id:$pid,name:$name}) RETURN c.profile_json AS profile',
            pid=str(r.project), name='周砚')).single()
        assert '同名新人物' in row['profile']


@pytest.mark.asyncio
async def test_failed_pg_commit_rolls_back_cleanup_and_journal_then_replays(env, monkeypatch):
    from contextlib import asynccontextmanager
    r, old_id, token = await prepare(env, monkeypatch)
    @asynccontextmanager
    async def failing_sessions():
        async with r.sessions() as db:
            def fail(session): raise RuntimeError('commit failed')
            event.listen(db.sync_session, 'before_commit', fail)
            try: yield db
            finally: event.remove(db.sync_session, 'before_commit', fail)
    monkeypatch.setattr('app.db.session.async_session_factory', failing_sessions)
    with pytest.raises(RuntimeError):
        await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0, caller='test', strict=True, graph_driver=env.driver)
    async with r.sessions() as db:
        assert await db.get(Character, old_id) is not None
        assert await db.get(AppliedCharacterDeletion, (token, r.project)) is None
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0, caller='test', strict=True, graph_driver=env.driver)
    async with r.sessions() as db:
        assert await db.get(Character, old_id) is None
        assert await db.get(AppliedCharacterDeletion, (token, r.project)) is not None


@pytest.mark.asyncio
async def test_delete_preserves_a_state_shared_with_another_character(env):
    pid = str(env.r.project)
    async with env.driver.session() as graph:
        result = await graph.run('CREATE (a:Character {project_id:$pid,name:"删除目标",profile_json:"{}"}), '
            '(b:Character {project_id:$pid,name:"保留人物"}), (s:CharacterState {status_json:"{}"}), '
            '(a)-[:HAS_STATE]->(s),(b)-[:HAS_STATE]->(s)', pid=pid)
        await result.consume()
    await delete_character_graph(env.driver, pid, '删除目标', {}, uuid4())
    async with env.driver.session() as graph:
        row = await (await graph.run('MATCH (:Character {project_id:$pid,name:"保留人物"})-[:HAS_STATE]->(s) '
            'RETURN count(s) AS n', pid=pid)).single()
    assert row['n'] == 1
