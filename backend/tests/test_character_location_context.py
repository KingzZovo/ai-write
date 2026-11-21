"""Actual writer cards must retain verified locations without guessing order."""
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
import json

import pytest
from sqlalchemy import insert, update

from app.models.project import Chapter, Character, CharacterLocation, CharacterState, Location, Project
from app.services.context_pack import CharacterCard, ContextPack, ContextPackBuilder
from app.services.context_errors import RequiredContextUnavailable
from app.services.context_budget import ContextBudgetExceeded
from tests.support.character_graph import CharacterGraph
from tests.test_character_state_context import state_db, add_state, receipt


def add_location(r, source, name, *, end=None):
    identity = uuid4()
    r.session.execute(insert(Location).values(id=identity, project_id=r.pid, name=name))
    r.session.execute(insert(CharacterLocation).values(project_id=r.pid, character_id=r.character_id,
        location_id=identity, chapter_start=source.global_idx, chapter_end=end,
        source_chapter_id=UUID(source.chapter_id), source_hash=source.source_hash))
    r.session.commit()


async def cards(r, monkeypatch, *, before=903, graph=None):
    """Run actual SQL, graph source checks and card/state assembly together."""
    class CardsBuilt(Exception): pass
    graph = graph or CharacterGraph(r.pid)
    async def driver(): yield graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    monkeypatch.setattr('app.services.character_roster.load_historical_roster', AsyncMock(return_value=[]))
    builder = ContextPackBuilder(db=r.db)
    monkeypatch.setattr(builder, '_cap_character_cards', AsyncMock(side_effect=CardsBuilt))
    pack = ContextPack(history_before_global_idx=before)
    with pytest.raises(CardsBuilt): await builder._build_facts(pack, r.pid, 4, before)
    return pack


@pytest.mark.asyncio
async def test_invalid_later_pg_source_cannot_hide_the_last_valid_location(state_db, monkeypatch):
    r = state_db
    earlier = await add_state(r, 900, {}, body='陈川留在船舱核对账目。')
    later = await add_state(r, 901, {}, body='陈川已经上岸取药。')
    add_location(r, earlier, '船舱', end=900)
    add_location(r, later, '岸边')
    r.session.execute(update(Chapter).where(Chapter.id == UUID(later.chapter_id)).values(content_text='陈川没有上岸。'))
    r.session.commit()
    pack = await cards(r, monkeypatch)
    card = next(card for card in pack.character_cards if card.name == '陈川')
    assert card.location == '船舱'
    assert '船舱' in pack.to_system_prompt() and '岸边' not in card.to_prompt() and '家中' not in card.to_prompt()


@pytest.mark.asyncio
async def test_verified_graph_location_enters_a_location_only_character_card(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {}, body='林禾仍在码头等陈川。')
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='林禾', location='码头', chapter_end=None, **receipt(source))]
    pack = await cards(r, monkeypatch, graph=graph)
    found = [card for card in pack.character_cards if card.name == '林禾']
    assert len(found) == 1 and found[0].location == '码头'
    assert '码头' in pack.to_system_prompt() and '[CH-901]' in found[0].to_prompt()


@pytest.mark.asyncio
async def test_same_chapter_pg_locations_keep_both_without_a_current_location_guess(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川从码头回到船舱，又有人喊他去了岸边。')
    add_location(r, source, '船舱')
    add_location(r, source, '岸边')
    pack = await cards(r, monkeypatch)
    card = pack.character_cards[0]
    assert card.location == ''
    assert all(place in card.to_prompt() for place in ('船舱', '岸边'))
    assert '不能按列表顺序决定当前位置' in card.to_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('pg_index,graph_index,expected', [(900, 901, '码头'), (901, 900, '船舱')])
async def test_graph_and_pg_locations_follow_source_chapters_not_store_priority(
        state_db, monkeypatch, pg_index, graph_index, expected):
    r = state_db
    pg = await add_state(r, pg_index, {}, body='陈川在船舱核账。')
    graph_source = await add_state(r, graph_index, {}, body='陈川到码头等船。')
    add_location(r, pg, '船舱')
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='陈川', location='码头', **receipt(graph_source))]
    card = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert card.location == expected and card.location_source['global_idx'] == 901
    assert len(card.chapter_locations) == 1


@pytest.mark.asyncio
async def test_same_chapter_cross_store_records_deduplicate_only_exact_observations(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川从船舱走上甲板。')
    add_location(r, source, '船舱')
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='陈川', location=place, **receipt(source)) for place in ('船舱', '甲板', '甲板')]
    first = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert first.location == '' and len(first.chapter_locations) == 2
    assert first.to_prompt().count('位置[CH-901]:船舱') == 1
    graph.locations.reverse()
    second = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert first.chapter_locations == second.chapter_locations and first.to_prompt() == second.to_prompt()


@pytest.mark.asyncio
async def test_same_field_pg_and_graph_places_remain_observations_not_a_store_choice(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {'位置': '船舱'}, body='陈川从船舱走到甲板，又回去拿借据。')
    graph = CharacterGraph(r.pid)
    graph.states = [dict(name='陈川', status=json.dumps({'位置': '甲板'}), **receipt(source))]
    card = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert card.location == ''
    assert all(place in card.to_prompt() for place in ('船舱', '甲板'))
    assert '不能按列表顺序决定当前位置' in card.to_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('store', ['pg', 'graph'])
async def test_same_field_multiple_state_rows_preserve_all_same_chapter_places(state_db, monkeypatch, store):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川往返船舱和甲板。')
    graph = CharacterGraph(r.pid)
    values = ['船舱', '甲板']
    if store == 'pg':
        r.session.execute(insert(CharacterState), [dict(project_id=r.pid, character_id=r.character_id,
            status_json={'位置': value}, source_mode='chapter_delta', chapter_start=source.global_idx,
            source_chapter_id=UUID(source.chapter_id), source_hash=source.source_hash) for value in values])
        r.session.commit()
    else:
        graph.states = [dict(name='陈川', status=json.dumps({'位置': value}), **receipt(source)) for value in values]
    card = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert card.location == ''
    assert all(value in card.to_prompt() for value in values)
    assert '不能按列表顺序决定当前位置' in card.to_prompt()


@pytest.mark.asyncio
async def test_position_and_state_on_different_same_named_graph_nodes_are_not_combined(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {}, body='船舱里的陈川受了伤，甲板上的同名人没有受伤。')
    graph = CharacterGraph(r.pid)
    graph.states = [dict(name='陈川', owner_node_id='injured-person', status=json.dumps({'伤势': '左手受伤'}),
                         **receipt(source))]
    graph.locations = [dict(name='陈川', owner_node_id='other-person', location='甲板', **receipt(source))]
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await cards(r, monkeypatch, graph=graph)


@pytest.mark.asyncio
async def test_same_named_pg_people_with_only_state_positions_require_identity_check(state_db, monkeypatch):
    r = state_db
    await add_state(r, 901, {'位置': '船舱'})
    r.session.execute(insert(Character).values(id=uuid4(), project_id=r.pid, name='陈川',
        profile_json={'身份': '另一位同名人'}))
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await cards(r, monkeypatch)


@pytest.mark.asyncio
async def test_invalid_closing_graph_source_does_not_erase_prior_writer_location(state_db, monkeypatch):
    r = state_db
    earlier = await add_state(r, 900, {}, body='陈川留在船舱。')
    later = await add_state(r, 901, {}, body='陈川已经上岸。')
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='陈川', location='船舱', chapter_end=900, **receipt(earlier)),
                       dict(name='陈川', location='岸边', **receipt(later))]
    r.session.execute(update(Chapter).where(Chapter.id == UUID(later.chapter_id)).values(content_text='陈川没有离船。'))
    r.session.commit()
    card = (await cards(r, monkeypatch, graph=graph)).character_cards[0]
    assert card.location == '船舱' and '岸边' not in card.to_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['future', 'hash', 'missing_source', 'foreign_place'])
async def test_unusable_pg_location_is_not_promoted_to_a_prose_fact(state_db, monkeypatch, damage):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川的行程仍待核实。')
    add_location(r, source, '不可采用的地点')
    if damage == 'foreign_place':
        other = uuid4()
        r.session.execute(insert(Project).values(id=other, title='其他书'))
        r.session.execute(update(Location).values(project_id=other))
    elif damage == 'hash': r.session.execute(update(CharacterLocation).values(source_hash='f' * 64))
    elif damage == 'missing_source': r.session.execute(update(CharacterLocation).values(source_chapter_id=None))
    r.session.commit()
    pack = await cards(r, monkeypatch, before=901 if damage == 'future' else 903)
    assert not pack.character_cards[0].chapter_locations
    assert '不可采用的地点' not in pack.to_system_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('place', [None, '', '  ', []])
async def test_malformed_current_graph_location_stops_the_writer(state_db, monkeypatch, place):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川仍在船舱。')
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='陈川', location=place, **receipt(source))]
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await cards(r, monkeypatch, graph=graph)


@pytest.mark.asyncio
async def test_actual_location_assembly_keeps_uncommitted_author_edits_untouched(state_db, monkeypatch):
    r = state_db
    source = await add_state(r, 901, {}, body='陈川在船舱。')
    add_location(r, source, '船舱')
    chapter = r.session.get(Chapter, UUID(source.chapter_id))
    chapter.content_text = '未提交的作者新稿。'
    flush = Mock(side_effect=AssertionError('read must not flush'))
    monkeypatch.setattr(r.session, 'flush', flush)
    pack = await cards(r, monkeypatch)
    assert pack.character_cards[0].location == '船舱'
    assert chapter.content_text == '未提交的作者新稿。'
    flush.assert_not_called()


def test_conflicting_same_chapter_state_location_fields_do_not_create_a_scalar_guess():
    source = dict(global_idx=901, source_chapter_id=str(uuid4()), source_hash='verified-source')
    card = CharacterCard(name='陈川')
    card.apply_chapter_state({'location': '船舱', '位置': '岸边'}, {'location': source, '位置': source})
    assert card.location == ''
    assert '船舱' in card.to_prompt() and '岸边' in card.to_prompt()
    assert '不能按列表顺序决定当前位置' in card.to_prompt()


@pytest.mark.asyncio
async def test_complete_same_chapter_places_are_required_material_not_a_silent_limit(state_db, monkeypatch):
    r = state_db
    places = [f'登记地点{index:04}' for index in range(2005)]
    source = await add_state(r, 901, {}, body='陈川走过' + '、'.join(places))
    graph = CharacterGraph(r.pid)
    graph.locations = [dict(name='陈川', location=place, **receipt(source)) for place in places]
    pack = await cards(r, monkeypatch, graph=graph)
    assert len(pack.character_cards[0].chapter_locations) == 2005
    assert places[-1] in pack.character_cards[0].to_prompt()
    with pytest.raises(ContextBudgetExceeded): pack.to_system_prompt(token_budget=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize('store', ['pg', 'graph'])
async def test_different_people_with_the_same_name_cannot_share_a_location_history(state_db, monkeypatch, store):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川留在船舱，另一位同名的陈川在码头做工。')
    last = await add_state(r, 901, {}, body='码头上的陈川留下了。')
    graph = CharacterGraph(r.pid)
    if store == 'pg':
        other = uuid4()
        r.session.execute(insert(Character).values(id=other, project_id=r.pid, name='陈川', profile_json={'身份': '同名的另一人'}))
        r.session.commit()
        add_location(r, first, '船舱')
    else:
        graph.locations = [dict(name='陈川', owner_node_id='first-person', location='船舱', **receipt(first)),
                           dict(name='陈川', owner_node_id='second-person', location='码头', **receipt(last))]
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await cards(r, monkeypatch, graph=graph)
