from contextlib import asynccontextmanager
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select, update

from app.models.project import Chapter, Character, Project, Relationship
from app.services import relationship_writes as writes
from app.services.relationship_graph_write import apply_relationship_graph_write, read_relationship_snapshot
from app.services.relationship_write_contract import (
    make_relationship_command, RelationshipWriteConflict, RelationshipSyncPending,
    RelationshipWriteUnconfirmed, RelationshipProjectUnavailable,
)
from app.services.entity_graph_projection import project_entity_payload, invalidate_entity_projection
from app.services.chapter_entity_extraction import FIELDS
from app.services.context_pack import ContextPack, ContextPackBuilder, CharacterCard
from app.services.context_budget import ContextBudgetExceeded
from tests.relationship_graph_double import RelationshipGraph
from tests.test_character_state_context import state_db, add_state


@pytest_asyncio.fixture
async def author_db(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 900, {}, body='陈川与沈青结为同伴。')
    Relationship.__table__.create(r.session.get_bind())
    r.target_id, r.rel_id = uuid4(), uuid4()
    r.session.execute(insert(Character).values(id=r.target_id, project_id=r.pid, name='沈青', profile_json={}))
    r.evolution = [{'volume_id': str(r.vid), 'note': '作者的分卷演变记录'}]
    r.session.execute(insert(Relationship).values(id=r.rel_id, project_id=r.pid,
        source_id=r.character_id, target_id=r.target_id, rel_type='同伴', label='旧标签', note='旧备注',
        sentiment='neutral', evolution_json=r.evolution, since_volume_id=r.vid))
    r.session.commit()
    r.source = source
    r.graph = RelationshipGraph(r.pid, [dict(edge_id='edge-1', project_id=str(r.pid), source='陈川', target='沈青',
        properties=dict(type='同伴', chapter_start=900, chapter_end=None, source_chapter_id=source.chapter_id,
            source_hash=source.source_hash, event_kind='new', label='旧标签', note='旧备注', sentiment='neutral'))])
    execute = r.db.execute
    r.fail_commit = r.lose_sql_ack = False
    r.commits = 0
    async def commit():
        if r.fail_commit:
            r.fail_commit = False
            raise RuntimeError('private SQL commit failure')
        r.session.commit(); r.commits += 1
        if r.lose_sql_ack:
            r.lose_sql_ack = False
            raise ConnectionError('lost SQL commit acknowledgement')
    async def rollback(): r.session.rollback()
    r.db = NS(execute=execute, commit=commit, rollback=rollback)
    @asynccontextmanager
    async def sessions():
        try: yield r.db
        except BaseException:
            r.session.rollback()
            raise
    r.sessions = sessions
    monkeypatch.setattr(writes, 'require_materialization_lock', AsyncMock())
    return r


async def command(r, operation='update', changes=None, *, rid=None, version=None, project=None, rel_type='同伴'):
    snapshot = await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', rel_type)
    return make_relationship_command(rid or uuid4(), project or r.pid, '陈川', '沈青', rel_type, operation,
        None if operation == 'create' else version or snapshot['version'],
        changes if changes is not None else {'label': '暂时结伴'})


def current(r):
    row = r.session.execute(select(*Relationship.__table__.columns).where(Relationship.id == r.rel_id)).mappings().one_or_none()
    r.session.commit()
    return dict(row) if row else None


async def prompt(r, monkeypatch, *, budget=None):
    async def driver(): yield r.graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    pack = ContextPack(character_cards=[CharacterCard('陈川'), CharacterCard('沈青')])
    await ContextPackBuilder(db=r.db)._enrich_characters_from_neo4j(pack, str(r.pid), 902)
    return pack.to_system_prompt(token_budget=budget)


def payload(source):
    data = {field: [] for field in FIELDS}
    data['new_relationships'] = [dict(char1='陈川', char2='沈青', type='同伴', evidence=source.content)]
    return data


@pytest.mark.asyncio
async def test_author_change_is_labelled_separately_from_prose_and_preserves_full_metadata(author_db, monkeypatch):
    r = author_db
    cmd = await command(r, changes={'new_rel_type': '敌对（仍需共同查案）', 'label': '公开翻脸',
        'note': '不能再把二人的配合写成亲密无间。', 'sentiment': 'negative'})
    result = await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert result['status'] == 'saved' and result['request_id'] == cmd.id
    props = r.graph.edges[0]['properties']
    assert props['origin'] == 'author' and 'source_chapter_id' not in props and 'source_hash' not in props
    assert json.loads(props['author_basis_json'])['source_hash'] == r.source.source_hash
    text = await prompt(r, monkeypatch)
    assert '【作者关系设定】' in text and '不证明关系已在正文成立' in text
    assert '敌对（仍需共同查案）' in text and cmd.changes['note'] in text and 'negative' in text
    assert '[CH-900] 陈川 → 沈青：敌对' not in text
    row = current(r)
    assert row['label'] == '公开翻脸' and row['note'] == cmd.changes['note'] and row['sentiment'] == 'negative'


@pytest.mark.asyncio
async def test_type_rename_keeps_projection_identity_and_volume_history(author_db):
    r = author_db; original = current(r)
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, changes={'new_rel_type': '敌对'}))
    row = current(r)
    assert row['id'] == original['id'] and row['rel_type'] == '敌对'
    assert row['evolution_json'] == r.evolution and row['since_volume_id'] == r.vid
    assert row['created_at'] == original['created_at'] and row['note'] == '旧备注'


@pytest.mark.asyncio
async def test_old_request_retry_projects_current_values_without_reapplying_old_edits(author_db):
    r = author_db
    first = await command(r)
    await writes.apply_author_relationship_write(r.db, r.graph, first)
    second = await command(r, changes={'label': '作者后来的修订'})
    await writes.apply_author_relationship_write(r.db, r.graph, second)
    mutations = r.graph.mutations
    await writes.apply_author_relationship_write(r.db, r.graph, first)
    assert current(r)['label'] == '作者后来的修订'
    assert r.graph.mutations == mutations == 2 and len(r.graph.receipts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['sql', 'sql_ack', 'graph_ack', 'sync_ack'])
async def test_failed_acknowledgements_recover_by_projecting_current_graph_only(author_db, failure):
    r = author_db; cmd = await command(r)
    r.fail_commit = failure == 'sql'
    r.lose_sql_ack = failure == 'sql_ack'
    r.graph.lose_ack = failure == 'graph_ack'
    r.graph.fail_sync_ack = failure == 'sync_ack'
    if failure == 'sync_ack':
        assert (await writes.apply_author_relationship_write(r.db, r.graph, cmd))['status'] == 'saved'
    else:
        expected = RelationshipWriteUnconfirmed if failure == 'graph_ack' else RelationshipSyncPending
        with pytest.raises(expected): await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert r.graph.receipts[cmd.id]['sync_pending']
    later = await command(r, changes={'label': '后来的真实图谱值'})
    await apply_relationship_graph_write(r.graph, later)
    result = await writes.replay_relationship_writes(r.graph, sessions=r.sessions)
    assert result == {'examined': 2, 'completed': 2, 'failed': 0}
    assert current(r)['label'] == '后来的真实图谱值'
    assert r.graph.mutations == 2 and not any(x['sync_pending'] for x in r.graph.receipts.values())


@pytest.mark.asyncio
async def test_stale_version_and_reused_request_id_never_change_the_graph(author_db):
    r = author_db
    old = await command(r)
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, changes={'note': '先提交的修改'}))
    with pytest.raises(RelationshipWriteConflict, match='source_changed'):
        await writes.apply_author_relationship_write(r.db, r.graph, old)
    changed = await command(r, rid=old.id, changes={'label': '另一份请求'})
    with pytest.raises(RelationshipWriteConflict, match='request_id_reused'):
        await writes.apply_author_relationship_write(r.db, r.graph, changed)
    assert current(r)['note'] == '先提交的修改' and r.graph.mutations == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_graph_change_and_suppression_together(author_db):
    r = author_db; before = json.dumps(r.graph.edges, sort_keys=True)
    r.graph.fail_receipt = True
    with pytest.raises(RelationshipWriteUnconfirmed):
        await writes.apply_author_relationship_write(r.db, r.graph, await command(r))
    assert json.dumps(r.graph.edges, sort_keys=True) == before
    assert not r.graph.receipts and not r.graph.suppressions
    assert current(r)['label'] == '旧标签'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_old_chapter_reprojection_cannot_undo_an_author_decision(author_db, operation):
    r = author_db
    changes = {'new_rel_type': '敌对'} if operation == 'update' else {}
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, operation, changes))
    await project_entity_payload(r.graph, r.source, payload(r.source), 'payload-hash', 5)
    assert len(r.graph.suppressions) == 1
    assert [edge['properties']['type'] for edge in r.graph.edges] == (['敌对'] if operation == 'update' else [])
    if operation == 'update':
        assert r.graph.edges[0]['properties']['origin'] == 'author'
    assert any('RelationshipProjectGuard' in query for query, _ in r.graph.calls)


@pytest.mark.asyncio
async def test_deleting_an_old_extraction_does_not_ban_a_later_chapters_relationship(author_db):
    r = author_db
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, 'delete', {}))
    later = await add_state(r, 901, {}, body='陈川与沈青再次结为同伴。')
    await project_entity_payload(r.graph, later, payload(later), 'later-payload', 6)
    assert len(r.graph.edges) == 1
    assert r.graph.edges[0]['properties']['source_chapter_id'] == later.chapter_id


@pytest.mark.asyncio
async def test_source_invalidation_keeps_the_author_setting_and_its_original_basis(author_db, monkeypatch):
    r = author_db
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r))
    r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
    r.session.commit()
    await invalidate_entity_projection(r.graph, project_id=str(r.pid), chapter_id=r.source.chapter_id, current_hash=None, token=8)
    assert r.graph.edges[0]['properties']['origin'] == 'author'
    assert '[作者设定]' in await prompt(r, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['missing', 'deleted'])
async def test_unavailable_projects_are_checked_before_any_graph_mutation(author_db, reason):
    r = author_db
    cmd = await command(r, project=uuid4() if reason == 'missing' else None)
    if reason == 'deleted':
        from datetime import datetime, timezone
        r.session.execute(update(Project).where(Project.id == r.pid).values(deleted_at=datetime.now(timezone.utc)))
        r.session.commit()
    calls = len(r.graph.calls)
    with pytest.raises(RelationshipProjectUnavailable):
        await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert len(r.graph.calls) == calls and r.graph.mutations == 0


@pytest.mark.asyncio
async def test_explicit_metadata_clear_is_projected_and_unrelated_fields_survive(author_db):
    r = author_db
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, changes={'note': '', 'label': None}))
    row = current(r)
    assert row['note'] == row['label'] == '' and row['sentiment'] == 'neutral'
    assert row['evolution_json'] == r.evolution


@pytest.mark.asyncio
async def test_author_relationship_note_is_required_context_and_cannot_be_cut(author_db, monkeypatch):
    r = author_db
    note = '合作仅为查案，不能擅自交换彼此的秘密。' * 120
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, changes={'note': note}))
    with pytest.raises(ContextBudgetExceeded, match='relationship_records'):
        await prompt(r, monkeypatch, budget=500)
    assert note in await prompt(r, monkeypatch, budget=3000)


@pytest.mark.asyncio
async def test_delete_receipt_keeps_original_projection_volume_history_and_is_not_replaced_on_retry(author_db):
    r = author_db
    cmd = await command(r, 'delete', {})
    await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    history = json.loads(r.graph.receipts[cmd.id]['projection_before_json'])
    assert current(r) is None
    assert history['id'] == str(r.rel_id) and history['since_volume_id'] == str(r.vid)
    assert history['evolution_json'] == r.evolution and history['created_at']
    await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert json.loads(r.graph.receipts[cmd.id]['projection_before_json']) == history


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['add_relationship', 'update_relationship'])
@pytest.mark.parametrize('decision', ['unchanged', 'update', 'delete'])
async def test_unsourced_legacy_writes_cannot_change_owned_pairs(author_db, operation, decision):
    from app.services.entity_timeline import EntityTimelineService
    r = author_db
    if decision != 'unchanged':
        await writes.apply_author_relationship_write(r.db, r.graph,
            await command(r, decision, {} if decision == 'delete' else {'label': '作者修改'}))
    before = json.dumps(r.graph.edges, sort_keys=True)
    with pytest.raises(RelationshipWriteConflict, match='versioned_relationship_required'):
        await getattr(EntityTimelineService(r.graph, strict_writes=True), operation)(
            str(r.pid), '陈川', '沈青', '同伴', 905)
    assert json.dumps(r.graph.edges, sort_keys=True) == before


@pytest.mark.asyncio
async def test_same_outline_extraction_does_not_resurrect_an_author_deleted_relationship(author_db):
    from app.services.outline_settings_graph import write_settings_graph
    r = author_db
    outline = NS(project_id=r.pid, outline_id=uuid4(), digest='outline-version-1')
    r.graph.edges[0]['properties'] = dict(type='同伴', chapter_start=0,
        source_outline_id=str(outline.outline_id), source_outline_digest=outline.digest)
    await writes.apply_author_relationship_write(r.db, r.graph, await command(r, 'delete', {}))
    data = {'characters': [{'name': '陈川'}, {'name': '沈青'}],
        'relationships': [{'source_name': '陈川', 'target_name': '沈青', 'rel_type': '同伴'}]}
    await write_settings_graph(r.graph, outline, data, {})
    assert not r.graph.edges
    outline.digest = 'author-revised-outline'
    await write_settings_graph(r.graph, outline, data, {})
    assert len(r.graph.edges) == 1
    assert r.graph.edges[0]['properties']['source_outline_digest'] == outline.digest


@pytest.mark.asyncio
async def test_projection_updates_are_not_reported_as_new_relationships(author_db):
    from app.services.relationship_projection import upsert_relationship_projection
    r = author_db
    row = dict(project_id=r.pid, source_id=r.character_id, target_id=r.target_id,
        rel_type='同伴', note='新备注')
    assert await upsert_relationship_projection(r.db, [row]) == 0
    assert await upsert_relationship_projection(r.db, [dict(row, id=r.rel_id, rel_type='敌对')]) == 0
    assert await upsert_relationship_projection(r.db, [dict(row, rel_type='同事')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [[], {}, ['损坏的备注']])
async def test_malformed_source_metadata_cannot_become_an_empty_snapshot(author_db, value):
    r = author_db
    r.graph.edges[0]['properties']['note'] = value
    with pytest.raises(RelationshipWriteConflict, match='invalid_relationship_metadata'):
        await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', '同伴')


@pytest.mark.asyncio
async def test_empty_update_is_rejected_before_transferring_source_ownership(author_db):
    with pytest.raises(ValueError, match='empty_relationship_patch'):
        await command(author_db, changes={})
    assert author_db.graph.edges[0]['properties']['source_hash'] == author_db.source.source_hash


@pytest.mark.asyncio
async def test_snapshot_version_covers_legacy_extension_properties_and_explicit_empty_sentiment(author_db):
    r = author_db
    r.graph.edges[0]['properties'].update(legacy_evidence='旧登记原文', sentiment='')
    before = await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', '同伴')
    r.graph.edges[0]['properties']['legacy_evidence'] = '已修订的原文'
    after = await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', '同伴')
    assert before['version'] != after['version']
    assert after['fields']['sentiment'] == ''
    assert after['entries'][0]['properties']['legacy_evidence'] == '已修订的原文'


@pytest.mark.asyncio
async def test_pending_character_deletion_cannot_reuse_its_old_projection_identity(author_db):
    from app.services.entity_materialization_lock import MaterializationBusy
    r = author_db
    cmd = await command(r)
    r.graph.pending_characters.add('陈川')
    with pytest.raises(MaterializationBusy):
        await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert not r.graph.receipts and r.graph.mutations == 0
    assert current(r)['id'] == r.rel_id and current(r)['label'] == '旧标签'


@pytest.mark.asyncio
async def test_new_author_relationship_creates_missing_read_characters_in_the_same_commit(author_db):
    r = author_db
    cmd = make_relationship_command(uuid4(), r.pid, '陈川', '唐淮', '同事', 'create', None,
        {'note': '只在公事上往来'})
    result = await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert result['status'] == 'saved'
    target = r.session.execute(select(Character.id).where(Character.project_id == r.pid,
        Character.name == '唐淮')).scalar_one()
    relationship = r.session.execute(select(Relationship.note).where(Relationship.project_id == r.pid,
        Relationship.source_id == r.character_id, Relationship.target_id == target)).scalar_one()
    assert relationship == '只在公事上往来' and r.commits == 1


@pytest.mark.asyncio
async def test_unchanged_author_relationships_use_bounded_queries_for_a_long_book(author_db):
    from app.services.relationship_projection import upsert_relationship_projection
    r = author_db
    rows = [dict(id=uuid4(), project_id=r.pid, source_id=r.character_id, target_id=r.target_id,
        rel_type=f'role-{index}', note='作者备注') for index in range(1005)]
    r.session.execute(insert(Relationship), rows)
    r.session.commit()
    r.queries.clear()
    assert await upsert_relationship_projection(r.db, rows) == 0
    assert len(r.queries) <= 6
    assert not any(str(query).startswith('UPDATE ') for query in r.queries)


@pytest.mark.asyncio
async def test_projection_conflict_after_graph_commit_keeps_the_original_request_pending(author_db):
    r = author_db
    # A stale read-model occupant must not turn an already committed graph edit
    # into a definite rejection that lets the client discard its request ID.
    r.session.execute(insert(Relationship).values(id=uuid4(), project_id=r.pid,
        source_id=r.character_id, target_id=r.target_id, rel_type='敌对'))
    r.session.commit()
    cmd = await command(r, changes={'new_rel_type': '敌对'})
    with pytest.raises(RelationshipSyncPending):
        await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    assert r.graph.receipts[cmd.id]['outcome'] == 'applied' and r.graph.receipts[cmd.id]['sync_pending']
    assert r.graph.edges[0]['properties']['type'] == '敌对' and current(r)['rel_type'] == '同伴'


@pytest.mark.asyncio
async def test_cached_projection_validation_still_rejects_foreign_stable_ids(author_db):
    from app.services.relationship_projection import upsert_relationship_projection
    r = author_db
    with pytest.raises(RelationshipWriteConflict, match='relationship_projection_owner_changed'):
        await upsert_relationship_projection(r.db, [dict(id=r.rel_id, project_id=uuid4(),
            source_id=r.character_id, target_id=r.target_id, rel_type='敌对', note='错误的归属')])
    assert current(r)['rel_type'] == '同伴' and current(r)['note'] == '旧备注'


@pytest.mark.asyncio
async def test_edit_keeps_every_source_in_a_canonical_group_and_does_not_flatten_untouched_notes(author_db):
    from copy import deepcopy
    r = author_db
    second = deepcopy(r.graph.edges[0])
    second['edge_id'] = 'edge-2'
    second['properties'].update(type='同行', chapter_start=901, note='另一条来源的备注')
    r.graph.edges.append(second)
    before = await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', '同伴')
    assert len(before['entries']) == 2 and 'note' in before['mixed_fields']
    cmd = await command(r, changes={'label': '只统一标签'})
    await writes.apply_author_relationship_write(r.db, r.graph, cmd)
    after = await read_relationship_snapshot(r.graph, r.pid, '陈川', '沈青', '同伴')
    assert {row['properties']['note'] for row in after['entries']} == {'旧备注', '另一条来源的备注'}
    assert {row['properties']['type'] for row in after['entries']} == {'同伴', '同行'}
    assert len(json.loads(r.graph.receipts[cmd.id]['before_json'])) == 2
