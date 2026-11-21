"""Source-scoped interval behavior and author/replay interaction, without services."""
from copy import deepcopy
import json

import pytest

from app.services.chapter_entity_extraction import FIELDS
from app.services.entity_context import verified_relationship_candidates, verified_world_snapshot
from app.services.entity_graph_projection import (
    ENTITY_PROJECTION_VERSION, project_entity_payload, invalidate_entity_projection, repair_relationship_intervals,
)
from app.services.entity_sources import digest
from app.services.relationship_graph_write import apply_relationship_graph_write
from tests.relationship_graph_double import RelationshipGraph
from tests.test_character_state_context import state_db, add_state
from tests.test_relationship_author_writes import author_db, command
from tests.test_relationship_context import reference


def payload(source, *, new=(), changed=()):
    data = {key: [] for key in FIELDS}
    data['new_relationships'] = [dict(char1='陈川', char2='沈青', type=kind, evidence=source.content) for kind in new]
    data['changed_relationships'] = [dict(char1='陈川', char2='沈青', type=kind, evidence=source.content,
        **({'replaces': refs} if refs is not None else {})) for kind, refs in changed]
    return data


async def project(graph, source, data, token):
    return await project_entity_payload(graph, source, data, digest([source.source_hash, data]), token)


def active_types(graph, idx):
    return {row['properties']['type'] for row in graph.edges
            if row['properties']['chapter_start'] <= idx
            and (row['properties'].get('chapter_end') is None or row['properties']['chapter_end'] >= idx)}


@pytest.mark.asyncio
async def test_projection_closes_only_the_referenced_role_and_preserves_quotes_and_ids(state_db):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川与沈青是同事，也是朋友。')
    later = await add_state(r, 901, {}, body='陈川与沈青绝交了，差事照旧。')
    graph = RelationshipGraph(r.pid)
    await project(graph, first, payload(first, new=('同事', '朋友')), 1)
    await project(graph, later, payload(later, changed=[('绝交', [reference(first)])]), 2)
    assert active_types(graph, 900) == {'同事', '朋友'}
    assert active_types(graph, 901) == {'同事', '绝交'}
    for row in graph.edges:
        props = row['properties']
        source = later if props['type'] == '绝交' else first
        assert props['record_id'] == reference(source, props['type']) and props['evidence'] == source.content
    refs = await verified_relationship_candidates(r.db, graph, r.pid, 902)
    assert {row['type'] for row in refs} == {'同事', '绝交'}
    assert graph.markers[(str(r.pid), later.chapter_id)]['schema_version'] == ENTITY_PROJECTION_VERSION


@pytest.mark.asyncio
async def test_v2_projection_is_rebuilt_and_legacy_unscoped_ends_are_reopened(state_db):
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    graph = RelationshipGraph(r.pid)
    data = payload(later, changed=[('敌对', None)])
    await project(graph, first, payload(first, new=('同事', '朋友')), 1)
    await project(graph, later, data, 2)
    # Recreate the v2 shape: derived ends and no exact reference metadata.
    for row in graph.edges:
        props = row['properties']
        for key in ('record_id', 'evidence', 'replaces', 'raw_type'): props.pop(key, None)
        if props['chapter_start'] == 900: props['chapter_end'] = 900
    graph.markers[(str(r.pid), later.chapter_id)]['schema_version'] = 2
    assert active_types(graph, 901) == {'敌对'}
    assert await project(graph, later, data, 3)
    assert active_types(graph, 901) == {'同事', '朋友', '敌对'}
    assert not await project(graph, later, data, 4)
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert all(row.scope_pending for row in snapshot.relationships if row.rel_type != '敌对')


@pytest.mark.asyncio
async def test_graph_rebuild_keeps_id_and_never_redirects_a_stale_source_reference(state_db):
    from dataclasses import replace
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    graph = RelationshipGraph(r.pid)
    data = payload(first, new=['朋友'])
    await project(graph, first, data, 1)
    original = deepcopy(graph.edges[0])
    await project(graph, later, payload(later, changed=[('敌对', [reference(first)])]), 2)
    await invalidate_entity_projection(graph, project_id=str(r.pid), chapter_id=first.chapter_id, current_hash=None, token=3)
    await project(graph, first, data, 4)
    rebuilt = next(row for row in graph.edges if row['properties']['type'] == '朋友')
    assert original['edge_id'] != rebuilt['edge_id']
    assert original['properties']['record_id'] == rebuilt['properties']['record_id']
    assert active_types(graph, 901) == {'敌对'}
    changed_source = replace(first, source_hash='changed-source-hash')
    await project(graph, changed_source, payload(changed_source, new=['朋友']), 5)
    assert active_types(graph, 901) == {'朋友', '敌对'}


@pytest.mark.asyncio
async def test_invalidating_a_closing_event_reopens_just_derived_intervals(state_db):
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    graph = RelationshipGraph(r.pid)
    await project(graph, first, payload(first, new=('同事', '朋友')), 1)
    await project(graph, later, payload(later, changed=[('敌对', [reference(first)])]), 2)
    await invalidate_entity_projection(graph, project_id=str(r.pid), chapter_id=later.chapter_id, current_hash=None, token=3)
    assert active_types(graph, 901) == {'同事', '朋友'}


@pytest.mark.asyncio
async def test_author_changes_make_old_references_unresolved_without_overwriting_the_decision(author_db):
    r = author_db
    # Convert to v3 so the original source includes an exact ID and evidence.
    await project(r.graph, r.source, payload(r.source, new=['同伴']), 1)
    later = await add_state(r, 901, {}, body='陈川和沈青分开了。')
    rid = reference(r.source, '同伴')
    await project(r.graph, later, payload(later, changed=[('分开', [rid])]), 2)
    cmd = await command(r, changes={'new_rel_type': '同伴（只在公事上往来）'})
    await apply_relationship_graph_write(r.graph, cmd)
    author = next(row['properties'] for row in r.graph.edges if row['properties'].get('origin') == 'author')
    assert not {'record_id', 'evidence', 'replaces', 'source_hash'} & author.keys()
    assert json.loads(author['author_basis_json'])['record_id'] == rid
    before = deepcopy(author)
    # Replaying the old source cannot recreate the suppressed prose record.
    r.graph.markers[(str(r.pid), r.source.chapter_id)]['schema_version'] = 2
    await project(r.graph, r.source, payload(r.source, new=['同伴']), 3)
    assert before == next(row['properties'] for row in r.graph.edges if row['properties'].get('origin') == 'author')
    refs = await verified_relationship_candidates(r.db, r.graph, r.pid, 902)
    assert all(row['record_id'] != rid for row in refs)
    snapshot = await verified_world_snapshot(r.db, r.graph, r.pid, 902)
    assert next(row for row in snapshot.relationships if row.rel_type == '分开').unresolved_replaces == [rid]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['update', 'delete'])
async def test_author_decision_on_a_closing_event_reopens_its_prior_prose_role(author_db, operation):
    r = author_db
    later = await add_state(r, 901, {}, body='陈川和沈青反目了。')
    await project(r.graph, later, payload(later, changed=[('敌对', [reference(r.source, '同伴')])]), 1)
    assert '同伴' not in active_types(r.graph, 901)
    cmd = await command(r, operation=operation, changes={} if operation == 'delete' else {'note': '作者修订'}, rel_type='敌对')
    await apply_relationship_graph_write(r.graph, cmd)
    assert '同伴' in active_types(r.graph, 901)
    # The author decision is retained when the old closing source is replayed.
    r.graph.markers[(str(r.pid), later.chapter_id)]['schema_version'] = 2
    await project(r.graph, later, payload(later, changed=[('敌对', [reference(r.source, '同伴')])]), 2)
    assert '同伴' in active_types(r.graph, 901)


@pytest.mark.asyncio
async def test_backfill_is_batched_and_never_changes_explicit_author_dates(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    rows = [dict(edge_id=str(i), project_id=str(r.pid), source='陈川', target='沈青',
        properties=dict(type=f'完整关系{i}', chapter_start=900, chapter_end=None,
                        source_chapter_id=first.chapter_id, source_hash=first.source_hash)) for i in range(405)]
    rows.append(dict(edge_id='author', project_id=str(r.pid), source='陈川', target='沈青',
        properties=dict(type='作者设定', chapter_start=899, chapter_end=899, origin='author')))
    graph = RelationshipGraph(r.pid, rows)
    await repair_relationship_intervals(graph, str(r.pid), ['陈川'])
    assert all(row['properties'].get('record_id') for row in graph.edges[:-1])
    batches = [p['rows'] for query, p in graph.calls if 'SET r.record_id=row.record_id' in query]
    assert [len(batch) for batch in batches] == [200, 200, 5]
    assert graph.edges[-1] == rows[-1]
