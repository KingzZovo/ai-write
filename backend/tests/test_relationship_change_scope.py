"""Relationship changes end only identified, source-verified prior records."""
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from app.models.project import Chapter
from app.services.context_pack import ContextPack
from app.services.context_budget import ContextBudgetExceeded
from app.services.entity_context import verified_world_snapshot, verified_relationship_candidates
from app.services.entity_sources import resolve_entity_source
from app.services.relationship_context import fold_relationship_events, render_relationship_record
from tests.test_character_state_context import state_db, add_state
from tests.test_relationship_context import Graph, edge, reference


@pytest.mark.asyncio
async def test_legacy_change_does_not_erase_parallel_family_and_work_relationships(state_db):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川和沈青既是兄妹，也是同事和朋友。')
    later = await add_state(r, 901, {}, body='陈川和沈青反目了。')
    graph = Graph([edge(first, kind) for kind in ('兄妹', '同事', '朋友')]
                  + [edge(later, '敌对', event='change')])
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {row.rel_type for row in snapshot.relationships} == {'兄妹', '同事', '朋友', '敌对'}
    for rel in snapshot.relationships:
        text = render_relationship_record(rel)
        assert '待核对' in text
        if rel.rel_type != '敌对':
            assert rel.scope_pending['source_chapter_id'] == later.chapter_id
            assert rel.scope_pending['source_hash'] == later.source_hash


@pytest.mark.asyncio
async def test_only_explicitly_ended_friendship_disappears_and_full_quote_reaches_context(state_db):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川和沈青是兄妹、同事，也是朋友。')
    body = '陈川说：“以后别再提朋友二字；衙门的差事照旧。”'
    later = await add_state(r, 901, {}, body=body)
    graph = Graph([edge(first, kind) for kind in ('兄妹', '同事', '朋友')]
                  + [edge(later, '绝交', event='change', replaces=[reference(first)], evidence=body)])
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {row.rel_type for row in snapshot.relationships} == {'兄妹', '同事', '绝交'}
    ended = next(row for row in snapshot.relationships if row.rel_type == '绝交')
    assert not ended.unresolved_replaces and ended.replaces == [reference(first)]
    text = ContextPack(relationship_records=snapshot.relationships).to_system_prompt()
    assert body in text and '[CH-901]' in text


@pytest.mark.asyncio
async def test_explicit_empty_scope_keeps_other_roles_without_guessing_an_end(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    snapshot = await verified_world_snapshot(r.db,
        Graph([edge(first), edge(later, '发生争执', event='change', replaces=[])]), r.pid, 902)
    assert {row.rel_type for row in snapshot.relationships} == {'朋友', '发生争执'}
    assert all(row.scope_pending is None for row in snapshot.relationships)
    assert '未明确结束其他已登记关系' in '\n'.join(map(render_relationship_record, snapshot.relationships))


@pytest.mark.asyncio
async def test_new_observation_is_not_closed_by_a_reference_to_an_older_same_type(state_db):
    r = state_db
    first, second, third = [await add_state(r, idx, {}) for idx in (900, 901, 902)]
    snapshot = await verified_world_snapshot(r.db, Graph([
        edge(first), edge(second), edge(third, '反目', event='change', replaces=[reference(first)])]), r.pid, 903)
    assert {(row.rel_type, row.chapter_start) for row in snapshot.relationships} == {('朋友', 901), ('反目', 902)}
    change = next(row for row in snapshot.relationships if row.event_kind == 'change')
    assert change.unresolved_replaces == [reference(first)]
    assert '结束范围待核对' in render_relationship_record(change)


@pytest.mark.asyncio
async def test_same_chapter_events_can_share_an_exact_prior_end_target(state_db):
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    snapshot = await verified_world_snapshot(r.db, Graph([edge(first)] + [
        edge(later, kind, event='change', replaces=[reference(first)]) for kind in ('反目', '停止私人往来')]), r.pid, 902)
    assert {row.rel_type for row in snapshot.relationships} == {'反目', '停止私人往来'}
    assert all(not row.unresolved_replaces for row in snapshot.relationships)


@pytest.mark.asyncio
async def test_canonical_label_does_not_merge_different_full_descriptions_or_end_them_together(state_db):
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    descriptions = ('同伴（只到渡口为止）', '同伴（还要一起送信）')
    rid = reference(first, '同伴', raw=descriptions[0])
    snapshot = await verified_world_snapshot(r.db, Graph([
        edge(first, '同伴', raw=description) for description in descriptions] + [
        edge(later, '分开赶路', event='change', replaces=[rid])]), r.pid, 902)
    assert {row.raw_type or row.rel_type for row in snapshot.relationships} == {descriptions[1], '分开赶路'}


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['reverse', 'other_pair', 'other_project', 'future', 'same_chapter', 'fabricated'])
async def test_out_of_scope_reference_cannot_end_another_relationship(state_db, case):
    r = state_db
    first, later, future = [await add_state(r, idx, {}) for idx in (900, 901, 902)]
    target = edge(first)
    rid = reference(first)
    if case == 'reverse':
        target = edge(first, actor='沈青', target='陈川')
        rid = reference(first, actor='沈青', target='陈川')
    elif case == 'other_pair':
        target = edge(first, target='阿玉')
        rid = reference(first, target='阿玉')
    elif case == 'other_project':
        rid = reference(replace(first, project_id=str(uuid4())))
    elif case == 'future':
        target, rid = edge(future), reference(future)
    elif case == 'same_chapter':
        target, rid = edge(later), reference(later)
    else:
        rid = 'rel_' + '0' * 64
    snapshot = await verified_world_snapshot(r.db, Graph([
        target, edge(later, '敌对', event='change', replaces=[rid])]), r.pid, 903)
    assert {row.rel_type for row in snapshot.relationships} == {'朋友', '敌对'}
    assert next(row for row in snapshot.relationships if row.rel_type == '敌对').unresolved_replaces == [rid]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['body', 'chronology'])
async def test_reference_to_old_source_never_redirects_to_rebuilt_record(state_db, mutation):
    r = state_db
    first, later = [await add_state(r, idx, {}) for idx in (900, 901)]
    patch = {'content_text': '陈川向沈青道了声谢。'} if mutation == 'body' else {'global_idx': 899}
    r.session.execute(update(Chapter).where(Chapter.id == UUID(first.chapter_id)).values(**patch))
    r.session.commit()
    rebuilt = await resolve_entity_source(r.db, r.pid, 0, first.chapter_id)
    snapshot = await verified_world_snapshot(r.db, Graph([
        edge(rebuilt), edge(later, '敌对', event='change', replaces=[reference(first)])]), r.pid, 902)
    assert {row.rel_type for row in snapshot.relationships} == {'朋友', '敌对'}
    assert reference(rebuilt) != reference(first)
    assert next(row for row in snapshot.relationships if row.rel_type == '敌对').unresolved_replaces


@pytest.mark.asyncio
async def test_pending_legacy_scope_keeps_its_source_when_the_change_type_is_observed_again(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {}, body='陈川和沈青翻脸了。')
    last = await add_state(r, 902, {})
    snapshot = await verified_world_snapshot(r.db, Graph([
        edge(first), edge(later, '敌对', event='change', evidence=later.content), edge(last, '敌对')]), r.pid, 903)
    friend = next(row for row in snapshot.relationships if row.rel_type == '朋友')
    assert friend.scope_pending['source_hash'] == later.source_hash
    assert later.content in render_relationship_record(friend)
    assert '[CH-901]' in render_relationship_record(friend)


@pytest.mark.asyncio
async def test_candidate_loader_uses_verified_graph_history_and_reads_no_states_or_locations(state_db):
    r = state_db
    first, stale, current, future = [await add_state(r, idx, {}) for idx in (900, 901, 902, 903)]
    old = edge(first, end=900, evidence=first.content)
    graph = Graph([old, edge(stale, '对手'), edge(current, '同僚'), edge(future, '同乡')])
    r.session.execute(update(Chapter).where(Chapter.id == UUID(stale.chapter_id)).values(status='draft'))
    r.session.commit()
    candidates = await verified_relationship_candidates(r.db, graph, r.pid, 902)
    assert candidates == [dict(record_id=reference(first), char1='陈川', char2='沈青', type='朋友',
        source_chapter_id=first.chapter_id, source_hash=first.source_hash, global_idx=900,
        evidence=first.content, scope_pending=None)]
    assert len(graph.queries) == 1
    assert '[e:RELATES_TO]' in graph.queries[0][0] and 'LIMIT' not in graph.queries[0][0]


@pytest.mark.asyncio
async def test_invalid_quote_is_not_exposed_as_verified_evidence(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    graph = Graph([edge(first, evidence='正文没有这句话。')])
    assert await verified_relationship_candidates(r.db, graph, r.pid, 901) == []
    assert (await verified_world_snapshot(r.db, graph, r.pid, 901)).relationships == []


@pytest.mark.asyncio
async def test_necessary_scope_and_quote_are_budgeted_without_clipping(state_db):
    r = state_db
    body = '陈川与沈青绝交，衙门的差事照旧。' * 100 + '仍是兄妹。'
    source = await add_state(r, 900, {}, body=body)
    snapshot = await verified_world_snapshot(r.db, Graph([
        edge(source, '绝交', event='change', evidence=body, replaces=[])]), r.pid, 901)
    pack = ContextPack(relationship_records=snapshot.relationships)
    with pytest.raises(ContextBudgetExceeded, match='relationship_records'):
        pack.to_system_prompt(token_budget=500)
    assert body in pack.to_system_prompt(token_budget=2400)


@pytest.mark.asyncio
async def test_reference_identity_requires_unchanged_full_wording_and_source(state_db):
    source = await add_state(state_db, 900, {})
    record = edge(source)
    record['record_id'] = reference(source)
    assert fold_relationship_events([record], state_db.pid)[0].record_id == record['record_id']
    record['raw_type'] = '朋友（并不信任）'
    with pytest.raises(ValueError, match='relationship_record_identity_changed'):
        fold_relationship_events([record], state_db.pid)


def test_long_history_preserves_all_roles_and_ends_only_one_exact_occurrence():
    from time import perf_counter
    from app.services.relationship_identity import relationship_record_id
    pid = str(uuid4())
    records = [dict(name=f'人物{i}', target='沈青', rel_type='朋友', event_kind='new',
        global_idx=i + 1, source_chapter_id=str(uuid4()), source_hash=f'source-{i}') for i in range(5000)]
    target = records[2500]
    rid = relationship_record_id(pid, target['name'], '沈青', '朋友', target['source_chapter_id'], target['source_hash'])
    records.append(dict(name=target['name'], target='沈青', rel_type='绝交', event_kind='change',
        global_idx=5001, source_chapter_id=str(uuid4()), source_hash='last-source', replaces=[rid]))
    started = perf_counter()
    result = fold_relationship_events(records, pid)
    elapsed = perf_counter() - started
    assert len(result) == 5000
    selected = [rel for rel in result if rel.source == target['name']]
    assert [rel.rel_type for rel in selected] == ['绝交'] and not selected[0].unresolved_replaces
    assert sum(rel.rel_type == '朋友' for rel in result) == 4999
    print(f'RELATIONSHIP_FOLD_5000_SECONDS={elapsed:.6f}')
