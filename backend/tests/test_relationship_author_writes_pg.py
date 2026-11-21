"""Real PostgreSQL/Neo4j gates; intentionally skipped by the offline profile."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.project import Chapter, Relationship
from app.services.chapter_entity_extraction import FIELDS
from app.services.character_deletion import delete_character_graph
from app.services.context_pack import ContextPack, ContextPackBuilder, CharacterCard
from app.services.entity_graph_projection import project_entity_payload
from app.services.entity_materialization_lock import MaterializationBusy
from app.services.outline_settings_graph import write_settings_graph
from app.services.relationship_graph_write import apply_relationship_graph_write, read_relationship_snapshot
from app.services.relationship_write_contract import make_relationship_command, RelationshipSyncPending
from app.services.relationship_writes import apply_author_relationship_write, replay_relationship_writes
from tests.test_entity_invalidation import clean_env, change
from tests.test_entity_projection import env, source
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def author_env(clean_env):
    e = clean_env
    body = '陈川与沈青结为同伴。'
    await change(e, content_text=body)
    e.source = await source(e.r)
    e.payload = {key: [] for key in FIELDS}
    e.payload['new_relationships'] = [dict(char1='陈川', char2='沈青', type='同伴', evidence=body)]
    await project_entity_payload(e.driver, e.source, e.payload, 'initial-payload', 1)
    await e.materialize(project_id=str(e.r.project), chapter_idx=1, caller='test.relationship',
                        strict=True, graph_driver=e.driver)
    async with e.r.sessions() as db:
        relationship = await db.scalar(select(Relationship).where(Relationship.project_id == e.r.project))
        e.original_id = relationship.id
        relationship.evolution_json = [{'label': '原有分卷关系演变'}]
        e.volume_id = (await db.get(Chapter, e.r.chapter)).volume_id
        relationship.since_volume_id = e.volume_id
        await db.commit()
    return e


async def command(e, operation='update', changes=None, rel_type='同伴'):
    snapshot = await read_relationship_snapshot(e.driver, e.r.project, '陈川', '沈青', rel_type)
    return make_relationship_command(uuid4(), e.r.project, '陈川', '沈青', rel_type, operation,
        None if operation == 'create' else snapshot['version'],
        changes if changes is not None else {'label': '作者修订', 'note': '合作仅限本案。'})


async def apply(e, cmd):
    async with e.r.sessions() as db:
        return await apply_author_relationship_write(db, e.driver, cmd)


@pytest.mark.asyncio
async def test_real_rename_preserves_identity_and_old_source_replay_cannot_undo_it(author_env, monkeypatch):
    e = author_env
    cmd = await command(e, changes={'new_rel_type': '敌对（尚需合作）', 'note': '不能替对方作出决定。'})
    assert (await apply(e, cmd))['status'] == 'saved'
    async with e.r.sessions() as db:
        row = await db.get(Relationship, e.original_id)
        assert row.rel_type == '敌对' and row.evolution_json == [{'label': '原有分卷关系演变'}]
        assert row.since_volume_id == e.volume_id
    await project_entity_payload(e.driver, e.source, e.payload, 'forced-replay', 2)
    snapshot = await read_relationship_snapshot(e.driver, e.r.project, '陈川', '沈青', '敌对')
    assert len(snapshot['entries']) == 1 and snapshot['entries'][0]['properties']['origin'] == 'author'
    assert not (await read_relationship_snapshot(e.driver, e.r.project, '陈川', '沈青', '同伴'))['entries']
    async def driver(): yield e.driver
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    async with e.r.sessions() as db:
        pack = ContextPack(character_cards=[CharacterCard('陈川'), CharacterCard('沈青')])
        await ContextPackBuilder(db=db)._enrich_characters_from_neo4j(pack, str(e.r.project), 2)
        prompt = pack.to_system_prompt()
        assert '[作者设定]' in prompt and '敌对（尚需合作）' in prompt and cmd.changes['note'] in prompt
        assert '[CH-1] 陈川 → 沈青：敌对' not in prompt


@pytest.mark.asyncio
async def test_real_delete_retains_history_and_retry_does_not_resurrect(author_env):
    e = author_env
    cmd = await command(e, 'delete', {})
    await apply(e, cmd)
    await project_entity_payload(e.driver, e.source, e.payload, 'forced-replay', 2)
    await apply(e, cmd)
    async with e.r.sessions() as db:
        assert await db.get(Relationship, e.original_id) is None
    async with e.driver.session() as session:
        row = await (await session.run('MATCH (w:RelationshipWrite {id:$id}) '
            'RETURN w.projection_before_json AS history,w.before_json AS graph', id=cmd.id)).single()
    assert json.loads(row['history'])['evolution_json'] == [{'label': '原有分卷关系演变'}]
    assert json.loads(row['graph'])[0]['properties']['source_hash'] == e.source.source_hash


@pytest.mark.asyncio
async def test_real_graph_version_lock_allows_only_one_of_two_stale_writers(author_env):
    e = author_env
    first = await command(e, changes={'label': '甲的修改'})
    second = await command(e, changes={'label': '乙的修改'})
    results = await asyncio.gather(*(apply_relationship_graph_write(e.driver, cmd,
        projection_id=str(e.original_id)) for cmd in (first, second)))
    assert sorted(row['outcome'] for row in results) == ['applied', 'rejected']
    winner = first if results[0]['outcome'] == 'applied' else second
    await apply(e, winner)
    async with e.r.sessions() as db:
        assert (await db.get(Relationship, e.original_id)).label == winner.changes['label']


@pytest.mark.asyncio
async def test_real_pg_commit_failure_recovers_current_graph_without_replaying_old_patch(author_env):
    e = author_env
    cmd = await command(e)
    async with e.r.sessions() as db:
        class FailCommit:
            def __getattr__(self, name): return getattr(db, name)
            async def commit(self): raise RuntimeError('injected commit failure')
        with pytest.raises(RelationshipSyncPending):
            await apply_author_relationship_write(FailCommit(), e.driver, cmd)
    newer = await command(e, changes={'label': '后来的作者决定'})
    await apply_relationship_graph_write(e.driver, newer)
    result = await replay_relationship_writes(e.driver, sessions=e.r.sessions)
    assert result['completed'] == 2 and result['failed'] == 0
    async with e.r.sessions() as db:
        assert (await db.get(Relationship, e.original_id)).label == '后来的作者决定'


@pytest.mark.asyncio
async def test_real_pending_character_deletion_cannot_attach_old_relationship_history_to_new_identity(author_env):
    e = author_env
    cmd = await command(e)
    await delete_character_graph(e.driver, e.r.project, '陈川', {}, uuid4())
    with pytest.raises(MaterializationBusy):
        await apply(e, cmd)
    await e.materialize(project_id=str(e.r.project), chapter_idx=1, caller='test.cleanup',
                        strict=True, graph_driver=e.driver)
    created = await command(e, 'create', {'label': '新登记'})
    await apply(e, created)
    async with e.r.sessions() as db:
        assert await db.get(Relationship, e.original_id) is None
        row = await db.scalar(select(Relationship).where(Relationship.project_id == e.r.project))
        assert row.id != e.original_id and row.evolution_json == []


@pytest.mark.asyncio
async def test_real_same_outline_replay_respects_deletion_of_its_relationship(author_env):
    e = author_env
    outline = SimpleNamespace(project_id=e.r.project, outline_id=uuid4(), digest='outline-v1')
    data = {'characters': [{'name': '陈川'}, {'name': '沈青'}],
        'relationships': [{'source_name': '陈川', 'target_name': '沈青', 'rel_type': '同事'}]}
    await write_settings_graph(e.driver, outline, data, {})
    await e.materialize(project_id=str(e.r.project), chapter_idx=1, caller='test.outline',
                        strict=True, graph_driver=e.driver)
    await apply(e, await command(e, 'delete', {}, rel_type='同事'))
    await write_settings_graph(e.driver, outline, data, {})
    assert not (await read_relationship_snapshot(e.driver, e.r.project, '陈川', '沈青', '同事'))['entries']
