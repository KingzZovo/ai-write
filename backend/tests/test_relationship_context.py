"""Relationship context uses real source-check SQL and a read-only graph double."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID
import re

import pytest
from sqlalchemy import delete, insert, update

from app.models.project import Chapter, Project, Volume
from app.services.context_errors import RequiredContextUnavailable
from app.services.context_pack import CharacterCard, ContextPack, ContextPackBuilder
from app.services.entity_context import verified_world_snapshot
from app.services.memory import HierarchicalMemory
from app.services.context_budget import ContextBudgetExceeded
from tests.test_character_state_context import state_db, add_state, receipt
from app.services.relationship_identity import relationship_record_id


def edge(source, kind='朋友', *, actor='陈川', target='沈青', event='new', end=None, raw=None,
         replaces=None, evidence=None):
    return dict(name=actor, target=target, rel_type=kind, event_kind=event,
                chapter_end=end, raw_type=raw, replaces=replaces, evidence=evidence, **receipt(source))


def reference(source, kind='朋友', *, actor='陈川', target='沈青', raw=None):
    return relationship_record_id(source.project_id, actor, target, kind,
                                  source.chapter_id, source.source_hash, raw)


class Graph:
    """Model only the selection clauses under test, not a Neo4j substitute.

    The source checks execute actual SQLite SQL. Real Cypher execution remains
    in external-service tests; assertions also check the emitted query shape.
    """
    def __init__(self, relationships, states=(), locations=()):
        self.records = dict(relationship=list(relationships), state=list(states), location=list(locations))
        self.queries = []

    def session(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def execute_read(self, callback):
        return await callback(self)

    async def run(self, query, **parameters):
        self.queries.append((query, parameters))
        kind = 'relationship' if '[e:RELATES_TO]' in query else 'location' if '[e:AT_LOCATION]' in query else 'state'
        rows = [row for row in self.records[kind] if row['global_idx'] < parameters['idx']]
        if 'pairs' in parameters:
            pairs = {tuple(pair) for pair in parameters['pairs']}
            rows = [row for row in rows if (row['name'], row['target']) in pairs]
        if 'coalesce(e.origin,"")<>"author"' in query:
            rows = [row for row in rows if row.get('origin') != 'author']
        if 'e.chapter_end IS NULL' in query:
            rows = [row for row in rows if row['chapter_end'] is None or row['chapter_end'] >= parameters['idx'] - 1]
        rows.sort(key=lambda row: (row['name'], -row['global_idx']))
        limit = re.search(r' LIMIT (\d+)', query)
        if limit:
            rows = rows[:int(limit[1])]

        async def records():
            for row in rows:
                yield row
        return records()


async def enrich(r, monkeypatch, graph, pack=None, *, before=902):
    async def driver():
        yield graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    pack = pack or ContextPack(character_cards=[CharacterCard(name='陈川')])
    await ContextPackBuilder(db=r.db)._enrich_characters_from_neo4j(pack, str(r.pid), before)
    return pack


@pytest.mark.asyncio
async def test_relationship_without_any_character_state_reaches_the_writing_prompt(state_db, monkeypatch):
    source = await add_state(state_db, 900, {}, body='陈川和沈青订下盟约。')
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '同盟')]))
    prompt = pack.to_system_prompt()
    assert '同盟' in prompt and '陈川 → 沈青' in prompt and '[CH-900]' in prompt


@pytest.mark.asyncio
async def test_parallel_roles_are_not_overwritten_by_the_last_dictionary_entry(state_db, monkeypatch):
    source = await add_state(state_db, 900, {}, body='陈川和沈青既是同事，也是朋友。')
    state = dict(name='陈川', status={'状态': '在场'}, chapter_end=None, **receipt(source))
    pack = await enrich(state_db, monkeypatch,
                        Graph([edge(source, '同事'), edge(source, '朋友')], [state]))
    prompt = pack.to_system_prompt()
    assert '同事' in prompt and '朋友' in prompt


@pytest.mark.asyncio
async def test_profile_relationship_cannot_hide_a_new_prose_relationship(state_db, monkeypatch):
    source = await add_state(state_db, 901, {}, body='陈川和沈青放下旧怨，订下盟约。')
    state = dict(name='陈川', status={}, chapter_end=None, **receipt(source))
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', relationships={'沈青': '旧怨'})])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '同盟')], [state]), pack)
    prompt = pack.to_system_prompt()
    assert '同盟' in prompt
    assert '档案关系参考' in prompt and '旧怨' in prompt


@pytest.mark.asyncio
async def test_stale_later_change_cannot_hide_valid_earlier_relationship(state_db):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川与沈青结为朋友。')
    later = await add_state(r, 901, {}, body='陈川与沈青反目。')
    graph = Graph([edge(first, end=900), edge(later, '敌对', event='change')])
    r.session.execute(update(Chapter).where(Chapter.id == UUID(later.chapter_id)).values(content_text='作者取消了反目的情节。'))
    r.session.commit()
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert [rel.rel_type for rel in snapshot.relationships] == ['朋友']


@pytest.mark.asyncio
async def test_incoming_relationship_survives_when_only_its_target_is_relevant(state_db, monkeypatch):
    source = await add_state(state_db, 900, {}, body='陈川仍然信任沈青。')
    state = dict(name='陈川', status={'情绪': '平静'}, chapter_end=None, **receipt(source))
    pack = ContextPack(current_outline={'summary': '沈青回城。'},
        character_cards=[CharacterCard(name='陈川'), CharacterCard(name='沈青')])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '信任')], [state]), pack)
    monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS', 1)
    await ContextPackBuilder(db=state_db.db)._cap_character_cards(pack, str(state_db.pid), historical_roster=[])
    assert [card.name for card in pack.character_cards] == ['沈青']
    prompt = pack.to_system_prompt()
    assert '陈川 → 沈青' in prompt and '信任' in prompt
    assert '沈青 → 陈川' not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['陈川', '沈青'])
async def test_relationship_aliases_retain_the_record_without_creating_extra_people(state_db, monkeypatch, selected):
    source = await add_state(state_db, 900, {}, body='老陈与阿青订下盟约。')
    pack = ContextPack(current_outline={'summary': f'{selected}独自回城。'},
        character_cards=[CharacterCard(name='陈川', aliases=['老陈']),
                         CharacterCard(name='沈青', aliases=['阿青'])])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '同盟', actor='老陈', target='阿青')]), pack)
    assert [card.name for card in pack.character_cards] == ['陈川', '沈青']
    monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS', 1)
    await ContextPackBuilder(db=state_db.db)._cap_character_cards(pack, str(state_db.pid), historical_roster=[])
    assert [card.name for card in pack.character_cards] == [selected]
    assert pack.relationship_aliases == {'老陈': ['陈川'], '阿青': ['沈青']}
    prompt = pack.to_system_prompt()
    assert '[CH-900] 老陈 → 阿青：同盟' in prompt
    assert '档案称呼核对' in prompt and '老陈：陈川' in prompt and '阿青：沈青' in prompt
    roster = prompt.split('=== 事实约束 ===', 1)[0]
    assert '陈川' in roster and '沈青' in roster
    assert '老陈' not in roster and '阿青' not in roster


@pytest.mark.asyncio
async def test_shared_relationship_alias_keeps_all_candidates_and_the_original_direction(state_db, monkeypatch):
    source = await add_state(state_db, 900, {}, body='老陈对沈青仍有疑心。')
    pack = ContextPack(current_outline={'summary': '陈川独自回城。'},
        character_cards=[CharacterCard(name='陈林', aliases=['老陈']),
                         CharacterCard(name='陈川', aliases=['老陈']),
                         CharacterCard(name='沈青')])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '怀疑', actor='老陈')]), pack)
    assert [card.name for card in pack.character_cards] == ['陈林', '陈川', '沈青']
    monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS', 1)
    await ContextPackBuilder(db=state_db.db)._cap_character_cards(pack, str(state_db.pid), historical_roster=[])
    assert [card.name for card in pack.character_cards] == ['陈川']
    assert pack.relationship_aliases == {'老陈': ['陈川', '陈林']}
    prompt = pack.to_system_prompt()
    assert '[CH-900] 老陈 → 沈青：怀疑' in prompt
    assert '老陈：陈川、陈林' in prompt and '多人登记' in prompt
    assert '陈川 → 沈青' not in prompt and '陈林 → 沈青' not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['陈川', '老陈'])
async def test_registered_name_takes_precedence_over_another_characters_alias(state_db, monkeypatch, selected):
    source = await add_state(state_db, 900, {}, body='老陈与沈青同乡。')
    pack = ContextPack(current_outline={'summary': f'{selected}独自回城。'},
        character_cards=[CharacterCard(name='陈川', aliases=['老陈']),
                         CharacterCard(name='老陈'), CharacterCard(name='沈青')])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '同乡', actor='老陈')]), pack)
    monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS', 1)
    await ContextPackBuilder(db=state_db.db)._cap_character_cards(pack, str(state_db.pid), historical_roster=[])
    assert [card.name for card in pack.character_cards] == [selected]
    assert pack.relationship_aliases == {}
    prompt = pack.to_system_prompt()
    if selected == '老陈':
        assert '[CH-900] 老陈 → 沈青：同乡' in prompt
    else:
        assert not pack.relationship_records and '【前文关系记录】' not in prompt


@pytest.mark.asyncio
async def test_more_than_two_thousand_relationships_are_not_silently_omitted(state_db):
    source = await add_state(state_db, 900, {})
    graph = Graph([edge(source, '同乡', target=f'配角{index:04d}') for index in range(2105)])
    snapshot = await verified_world_snapshot(state_db.db, graph, state_db.pid, 902)
    assert len(snapshot.relationships) == 2105
    query = next(query for query, _ in graph.queries if '[e:RELATES_TO]' in query)
    assert 'LIMIT 2000' not in query


@pytest.mark.asyncio
async def test_required_graph_failure_does_not_return_only_old_profile_relationships(state_db, monkeypatch):
    async def unavailable():
        raise RuntimeError('private graph failure')
        yield
    monkeypatch.setattr('app.db.neo4j.get_neo4j', unavailable)
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', relationships={'沈青': '旧同盟'})])
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await ContextPackBuilder(db=state_db.db)._enrich_characters_from_neo4j(pack, str(state_db.pid), 902)


@pytest.mark.asyncio
async def test_hierarchical_memory_preserves_direction_and_source_chapter(state_db):
    source = await add_state(state_db, 900, {}, body='陈川对沈青起了疑心。')
    graph = Graph([edge(source, '陈川怀疑沈青')])
    prompt = await HierarchicalMemory(state_db.db, graph)._gather_entity_states(str(state_db.pid), 902)
    assert '陈川 → 沈青' in prompt and '[CH-900]' in prompt
    assert '<->' not in prompt


@pytest.mark.asyncio
async def test_new_roles_coexist_but_an_explicit_later_change_replaces_earlier_types(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    graph = Graph([edge(first, '同事'), edge(first, '朋友'), edge(later, '敌对', event='change',
                   replaces=[reference(first, '同事'), reference(first, '朋友')])])
    before = await verified_world_snapshot(r.db, graph, r.pid, 901)
    after = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {rel.rel_type for rel in before.relationships} == {'同事', '朋友'}
    assert [(rel.rel_type, rel.chapter_start, rel.event_kind) for rel in after.relationships] == [('敌对', 901, 'change')]
    assert after.relationships[0].source_chapter_id == later.chapter_id
    assert after.relationships[0].source_hash == later.source_hash


@pytest.mark.asyncio
async def test_repeated_type_keeps_latest_source_without_losing_other_parallel_role(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    graph = Graph([edge(first, '同事'), edge(first, '朋友'), edge(later, '朋友')])
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {(rel.rel_type, rel.chapter_start) for rel in snapshot.relationships} == {('同事', 900), ('朋友', 901)}


@pytest.mark.asyncio
async def test_same_chapter_new_and_changed_roles_follow_the_writer_contract(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    graph = Graph([edge(first, '朋友'), edge(later, '同僚'), edge(later, '敌对', event='change',
                   replaces=[reference(first)])])
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {rel.rel_type for rel in snapshot.relationships} == {'同僚', '敌对'}


@pytest.mark.asyncio
async def test_a_directed_change_does_not_overwrite_the_reverse_observation(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    graph = Graph([edge(first, '信任'), edge(first, '试探', actor='沈青', target='陈川'),
                   edge(later, '开始怀疑', event='change', replaces=[reference(first, '信任')])])
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert {(rel.source, rel.target, rel.rel_type) for rel in snapshot.relationships} == {
        ('陈川', '沈青', '开始怀疑'), ('沈青', '陈川', '试探')}


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['draft', 'delete', 'reindex', 'hash', 'foreign', 'duplicate'])
async def test_invalid_closing_source_does_not_keep_earlier_relationship_closed(state_db, mutation):
    from uuid import uuid4
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    closing = edge(later, '敌对', event='change', replaces=[reference(first)])
    graph = Graph([edge(first, end=900), closing])
    cid = UUID(later.chapter_id)
    if mutation == 'draft':
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(status='draft'))
    elif mutation == 'delete':
        r.session.execute(delete(Chapter).where(Chapter.id == cid))
    elif mutation == 'reindex':
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(global_idx=903))
    elif mutation == 'hash':
        closing['source_hash'] = 'stale'
    elif mutation == 'duplicate':
        r.session.execute(insert(Chapter).values(volume_id=r.vid, title='重号章', chapter_idx=3,
            global_idx=901, status='draft'))
    else:
        pid, vid = uuid4(), uuid4()
        r.session.execute(insert(Project).values(id=pid, title='另一本书'))
        r.session.execute(insert(Volume).values(id=vid, project_id=pid, volume_idx=1, title='异书'))
        r.session.execute(update(Chapter).where(Chapter.id == cid).values(volume_id=vid))
    r.session.commit()
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert [(rel.rel_type, rel.chapter_start, rel.chapter_end) for rel in snapshot.relationships] == [('朋友', 900, None)]


@pytest.mark.asyncio
async def test_stale_earlier_role_is_not_restored_by_a_later_valid_parallel_role(state_db):
    r = state_db
    first = await add_state(r, 900, {})
    later = await add_state(r, 901, {})
    graph = Graph([edge(first, '朋友'), edge(later, '同僚')])
    r.session.execute(update(Chapter).where(Chapter.id == UUID(first.chapter_id)).values(content_text='改稿。'))
    r.session.commit()
    snapshot = await verified_world_snapshot(r.db, graph, r.pid, 902)
    assert [rel.rel_type for rel in snapshot.relationships] == ['同僚']


@pytest.mark.asyncio
async def test_full_raw_description_reaches_prompt_without_token_canonicalization(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    description = '曾经对立（如今停止追捕，但并未互相信任）/暂时同行'
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '对立', raw=description)]))
    prompt = pack.to_system_prompt()
    assert description in prompt
    assert '陈川 → 沈青：对立\n' not in prompt


@pytest.mark.asyncio
async def test_card_selection_keeps_both_names_of_relevant_relationship_only(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    pack = ContextPack(current_outline={'summary': '陈川继续等人。'},
        character_cards=[CharacterCard(name='陈川'), CharacterCard(name='沈青'),
                         CharacterCard(name='阿玉'), CharacterCard(name='江禾')])
    graph = Graph([edge(source, '同盟'), edge(source, '同乡', actor='阿玉', target='江禾')])
    pack = await enrich(state_db, monkeypatch, graph, pack)
    monkeypatch.setattr('app.config.settings.CONTEXT_PACK_MAX_CHARACTER_CARDS', 1)
    await ContextPackBuilder(db=state_db.db)._cap_character_cards(pack, str(state_db.pid), historical_roster=[])
    prompt = pack.to_system_prompt()
    roster = prompt.split('=== 事实约束 ===', 1)[0]
    assert '陈川、沈青' in roster
    assert len(pack.character_cards) == 1 and len(pack.relationship_records) == 1
    assert '江禾' not in prompt and '阿玉' not in prompt


@pytest.mark.asyncio
async def test_relationships_are_required_and_cannot_be_cut_into_a_different_meaning(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    description = '这份合作附有明确条件。' * 120 + '未形成私人信任。'
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, description)]))
    pack.rag_snippets = ['无关检索。' * 3000]
    with pytest.raises(ContextBudgetExceeded, match='relationship_records'):
        pack.to_system_prompt(token_budget=500)
    prompt = pack.to_system_prompt(token_budget=1600)
    assert description in prompt
    trace = {row['source']: row for row in pack.budget_trace}
    assert trace['relationship_records']['required'] and trace['relationship_records']['status'] == 'included'


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [None, '', 'unknown'])
async def test_unknown_relationship_event_kind_is_not_guessed(state_db, monkeypatch, event):
    source = await add_state(state_db, 900, {})
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await enrich(state_db, monkeypatch, Graph([edge(source, event=event)]))


@pytest.mark.asyncio
async def test_later_graph_query_failure_cannot_publish_partial_state_or_relationships(state_db, monkeypatch):
    from dataclasses import asdict
    source = await add_state(state_db, 900, {})
    state = dict(name='陈川', status={'伤势': '左手骨折'}, chapter_end=None, **receipt(source))
    graph = Graph([edge(source)], [state])
    original_run = graph.run
    async def fail_relationships(query, **parameters):
        if '[e:RELATES_TO]' in query:
            raise TimeoutError('private graph timeout')
        return await original_run(query, **parameters)
    graph.run = fail_relationships
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', relationships={'沈青': '同乡'})])
    original = asdict(pack)
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await enrich(state_db, monkeypatch, graph, pack)
    assert asdict(pack) == original


@pytest.mark.asyncio
async def test_absent_driver_is_not_an_empty_relationship_history(state_db, monkeypatch):
    async def driver():
        yield None
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await ContextPackBuilder(db=state_db.db)._enrich_characters_from_neo4j(ContextPack(), str(state_db.pid), 902)


@pytest.mark.asyncio
async def test_graph_read_does_not_mutate_the_author_profile_dictionary(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    profile = {'沈青': '旧怨'}
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', relationships=profile)])
    pack = await enrich(state_db, monkeypatch, Graph([edge(source, '同盟')]), pack)
    assert profile == {'沈青': '旧怨'}
    assert pack.character_cards[0].relationships == profile
    assert pack.character_cards[0].relationships is not profile
