"""Real Neo4j + PostgreSQL integration gates for outline settings."""
import json
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.models import Base
from app.models.project import Character, Outline
from app.services.outline_settings_graph import write_settings_graph
from app.services.outline_settings_source import read_settings_source, require_current_settings_source
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from tests.test_pipeline_control import runtime_db
from tests.test_entity_projection import env


@pytest.mark.asyncio
@pytest.mark.parametrize('preserve_existing', [True, False])
async def test_all_graph_characters_project_once_without_overwriting_author_cards(env, monkeypatch, preserve_existing):
    r = env.r
    async with r.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    async with r.sessions() as db:
        author = Character(project_id=r.project, name='周砚', profile_json={'role': '作者确认的师父'})
        book = Outline(project_id=r.project, level='book', content_json={'raw_text': '作者书纲'})
        db.add_all([author, book]); await db.commit()
        book_id = book.id
        existing = {'周砚': author}
    async with env.driver.session() as graph:
        await graph.run('CREATE (c:Character {project_id:$pid, name:$name, profile_json:$profile})',
            pid=str(r.project), name='周砚', profile=json.dumps({'role': '旧图资料'}))
    payload = {'characters': [{'name': '林禾', 'role': '没有关系也要保存的学徒'}, {'name': '周砚', 'role': '不得覆盖作者'}],
        'world_rules': [], 'relationships': []}
    for attempt in range(2):
        async with r.sessions() as reader:
            source = await read_settings_source(reader, r.project, book_id)
            await reader.rollback()
            await require_current_settings_source(reader, source)
            await write_settings_graph(env.driver, source, payload, existing)
            counts = await asyncio.wait_for(_materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0,
                caller='test.outline-settings', strict=True, graph_driver=env.driver,
                preserve_existing_profiles=preserve_existing), timeout=10)
            await reader.commit()
        assert counts['chars_created'] == (1 if attempt == 0 else 0)
    async with r.sessions() as db:
        chars = (await db.scalars(select(Character).where(Character.project_id == r.project))).all()
        by_name = {c.name: c for c in chars}
        assert by_name['周砚'].profile_json == {'role': '作者确认的师父'}
        assert by_name['林禾'].profile_json['role'] == '没有关系也要保存的学徒'
        assert len(chars) == 2


@pytest.mark.asyncio
async def test_explicit_author_profile_update_and_clear_reach_postgres(env, monkeypatch):
    r = env.r
    async with r.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    async with r.sessions() as db:
        db.add(Character(project_id=r.project, name='周砚', profile_json={'identity': '旧身份'}))
        await db.commit()
    for profile in ({'identity': '作者修订的师父', 'aliases': ['老周']}, {}):
        async with env.driver.session() as graph:
            result = await graph.run('MERGE (c:Character {project_id:$pid, name:$name}) '
                "SET c.profile_json=$profile,c.profile_origin='author_edit'",
                pid=str(r.project), name='周砚', profile=json.dumps(profile))
            await result.consume()
        await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0,
            caller='test.author-profile', strict=True, graph_driver=env.driver)
        async with r.sessions() as db:
            character = await db.scalar(select(Character).where(Character.project_id == r.project))
            assert character.profile_json == profile


@pytest.mark.asyncio
async def test_graph_profile_cas_allows_only_one_concurrent_author_revision(env):
    from app.services.character_profile_write import write_character_profile, CharacterProfileConflict
    driver, pid = env.driver, str(env.r.project)
    await write_character_profile(driver, pid, '周砚', {'identity': '原稿'}, None)
    results = await asyncio.wait_for(asyncio.gather(
        write_character_profile(driver, pid, '周砚', {'identity': '稿件甲'}, {'identity': '原稿'}),
        write_character_profile(driver, pid, '周砚', {'identity': '稿件乙'}, {'identity': '原稿'}),
        return_exceptions=True), 10)
    assert sum(isinstance(result, CharacterProfileConflict) for result in results) == 1
    assert sum(isinstance(result, str) for result in results) == 1
    async with driver.session() as graph:
        record = await (await graph.run('MATCH (c:Character {project_id:$pid,name:$name}) '
            'RETURN c.profile_json AS profile', pid=pid, name='周砚')).single()
    assert json.loads(record['profile']) in ({'identity': '稿件甲'}, {'identity': '稿件乙'})


@pytest.mark.asyncio
async def test_graph_profile_retry_and_delete_fence(env):
    from app.services.character_profile_write import write_character_profile, CharacterProfileConflict
    driver, pid = env.driver, str(env.r.project)
    await write_character_profile(driver, pid, '周砚', {'identity': '原稿'}, None)
    await write_character_profile(driver, pid, '周砚', {}, {'identity': '原稿'})
    await write_character_profile(driver, pid, '周砚', {}, {'identity': '原稿'})
    async with driver.session() as graph:
        result = await graph.run('MATCH (c:Character {project_id:$pid,name:$name}) DETACH DELETE c', pid=pid, name='周砚')
        await result.consume()
    with pytest.raises(CharacterProfileConflict):
        await write_character_profile(driver, pid, '周砚', {'identity': '不能复活旧人物'}, {})
    async with driver.session() as graph:
        record = await (await graph.run('MATCH (c:Character {project_id:$pid,name:$name}) RETURN count(c) AS n', pid=pid, name='周砚')).single()
    assert record['n'] == 0


@pytest.mark.asyncio
async def test_author_pending_marker_is_atomic_and_old_ack_cannot_clear_new_save(env):
    from app.services.character_profile_write import write_character_profile, CharacterProfileConflict
    from app.services.character_profile_recovery import acknowledge_profiles, defer_profiles
    driver, pid = env.driver, str(env.r.project)
    first = await write_character_profile(driver, pid, '周砚', {'identity': '原稿'}, None)
    second = await write_character_profile(driver, pid, '周砚', {'identity': '新稿'}, {'identity': '原稿'})
    assert first != second
    with pytest.raises(CharacterProfileConflict):
        await write_character_profile(driver, pid, '周砚', {'identity': '不能覆盖'}, {'identity': '原稿'})
    assert await acknowledge_profiles(driver, pid, [{'name': '周砚', 'token': first}]) == 0
    await defer_profiles(driver, pid, [{'name': '周砚', 'token': first}])
    async with driver.session() as graph:
        row = await (await graph.run('MATCH (c:Character {project_id:$pid,name:$name}) '
            'RETURN c.profile_json AS profile,c.profile_sync_token AS token,c.profile_sync_after AS retry_at',
            pid=pid, name='周砚')).single()
    assert json.loads(row['profile']) == {'identity': '新稿'}
    assert row['token'] == second and row['retry_at'] == 0
    from app.api.neo4j_settings import character_profile_status
    assert await character_profile_status(pid, '周砚', driver) == {
        'name': '周砚', 'profile_json': {'identity': '新稿'}, 'pending': True}
    assert await acknowledge_profiles(driver, pid, [{'name': '周砚', 'token': second}]) == 1
    assert (await character_profile_status(pid, '周砚', driver))['pending'] is False


@pytest.mark.asyncio
async def test_pending_author_save_can_be_projected_and_acknowledged_after_initial_failure(env, monkeypatch):
    from app.services.character_profile_write import write_character_profile
    from app.services.character_profile_recovery import acknowledge_profiles
    r = env.r
    async with r.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    token = await write_character_profile(env.driver, str(r.project), '周砚', {'identity': '待同步的师父'}, None)
    # Simulate process death after graph commit: a new call projects the retained
    # canonical graph, without repeating the author write or calling a model.
    await _materialize_entities_to_postgres(project_id=str(r.project), chapter_idx=0,
        caller='test.author-recovery', strict=True, graph_driver=env.driver)
    assert await acknowledge_profiles(env.driver, r.project, [{'name': '周砚', 'token': token}]) == 1
    async with r.sessions() as db:
        character = await db.scalar(select(Character).where(Character.project_id == r.project))
        assert character.profile_json == {'identity': '待同步的师父'}


@pytest.mark.asyncio
async def test_graph_transaction_failure_rolls_back_all_settings_nodes(env):
    source = SimpleNamespace(project_id=env.r.project, outline_id=uuid4(), digest='rollback-test')
    class Session:
        async def __aenter__(self):
            self.real = env.driver.session()
            await self.real.__aenter__()
            return self
        async def __aexit__(self, *args): await self.real.__aexit__(*args)
        async def execute_write(self, write):
            async def fail(tx):
                await write(tx)
                raise RuntimeError('fail before graph commit')
            await self.real.execute_write(fail)
    with pytest.raises(RuntimeError):
        await write_settings_graph(SimpleNamespace(session=lambda: Session()), source,
            {'characters': [{'name': '不得留存'}], 'world_rules': [], 'relationships': []}, {})
    async with env.driver.session() as graph:
        result = await graph.run('MATCH (c:Character {project_id:$pid, name:$name}) RETURN count(c) AS n',
            pid=str(env.r.project), name='不得留存')
        record = await result.single()
        assert record['n'] == 0


@pytest.mark.asyncio
async def test_durable_delivery_recovers_after_graph_commit_without_duplicate_characters(env, monkeypatch):
    from unittest.mock import AsyncMock
    from app.models.entity_extraction import OutlineSettingsExtraction as Receipt
    from app.services.outline_settings_delivery import save_receipt, deliver_receipt, apply_payload
    r = env.r
    async with r.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    async with r.sessions() as db:
        book = Outline(project_id=r.project, level='book', content_json={'raw_text': '林禾修补旧书。'})
        db.add(book)
        await db.commit()
        source = await read_settings_source(db, r.project, book.id)
        receipt = await save_receipt(db, source, {'characters': [{'name': '林禾', 'role': '学徒'}],
            'world_rules': [], 'relationships': []})
        rid = receipt.id
        async def fail_projection(db, source, payload):
            return await apply_payload(db, source, payload, driver=env.driver,
                materialize=AsyncMock(side_effect=RuntimeError('projection unavailable')))
        assert (await deliver_receipt(db, receipt, apply=fail_projection)).status == 'failed'
    async with r.sessions() as db:
        assert not (await db.scalars(select(Character).where(Character.project_id == r.project))).all()
        receipt = await db.get(Receipt, rid)
        async def apply(db, source, payload):
            return await apply_payload(db, source, payload, driver=env.driver)
        assert (await asyncio.wait_for(deliver_receipt(db, receipt, apply=apply), 10)).status == 'completed'
    async with r.sessions() as db:
        chars = (await db.scalars(select(Character).where(Character.project_id == r.project))).all()
        assert len(chars) == 1 and chars[0].profile_json['role'] == '学徒'
    async with env.driver.session() as graph:
        result = await graph.run('MATCH (c:Character {project_id:$pid, name:$name}) RETURN count(c) AS n',
            pid=str(r.project), name='林禾')
        assert (await result.single())['n'] == 1
