from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.context_errors import RequiredContextUnavailable
from app.services.world_rule_material import WorldRuleSourceError
from tests.support.world_rules import install_rule_graph


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['world_rules', 'character_cards'])
@pytest.mark.parametrize('error', [RuntimeError('private source error'), SQLAlchemyError('db unavailable')])
async def test_required_fact_reads_do_not_return_an_incomplete_pack(monkeypatch, stage, error):
    pid = uuid4()
    graph = install_rule_graph(monkeypatch, pid)
    if stage == 'world_rules':
        graph.rules.append({'category': '渡口', 'text': '封航期间不能过河。',
                            'source_outline_id': str(uuid4()), 'source_outline_material_hash': 'a' * 64})
    empty = NS(all=lambda: [])
    db = NS(execute=AsyncMock(side_effect=([empty] if stage == 'character_cards' else []) + [error]))
    builder = ContextPackBuilder(db=db)
    expected = SQLAlchemyError if isinstance(error, SQLAlchemyError) else RequiredContextUnavailable
    with pytest.raises(expected) as caught:
        await builder._build_facts(ContextPack(), pid, 2, global_chapter_idx=10)
    if expected is RequiredContextUnavailable:
        if stage == 'world_rules':
            assert isinstance(caught.value, WorldRuleSourceError)
            assert caught.value.code == 'world_rules_context_unavailable'
            assert 'private' not in str(caught.value)
        else:
            assert str(caught.value) == 'character_cards_context_unavailable'
            assert caught.value.__cause__ is error


@pytest.mark.asyncio
async def test_identity_contract_failure_stops_build_before_optional_retrieval(monkeypatch):
    from app.services import context_pack as module, ctxpack_cache, memory_chronology
    monkeypatch.setattr(ctxpack_cache, 'is_invalid', AsyncMock(return_value=False))
    monkeypatch.setattr(memory_chronology, 'resolve_history_boundary', AsyncMock(return_value=10))
    failure = RuntimeError('private identity source')
    monkeypatch.setattr(module, 'build_fact_contract', AsyncMock(side_effect=failure))
    builder = ContextPackBuilder(db=NS())
    monkeypatch.setattr(builder, '_resolve_global_chapter_idx', AsyncMock(return_value=10))
    monkeypatch.setattr(builder, '_build_proximity', AsyncMock())
    monkeypatch.setattr(builder, '_build_facts', AsyncMock())
    rag = AsyncMock()
    monkeypatch.setattr(builder, '_build_rag', rag)
    with pytest.raises(RequiredContextUnavailable, match='character_identity_context_unavailable') as caught:
        await builder.build(project_id=uuid4(), volume_id=uuid4(), chapter_idx=2)
    assert caught.value.__cause__ is failure
    rag.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_later_profile_cannot_silently_leave_only_earlier_cards(monkeypatch):
    pid = uuid4()
    install_rule_graph(monkeypatch, pid)
    empty = NS(all=lambda: [])
    chars = [NS(id=uuid4(), name='陈川', profile_json={'location': '门外'}),
             NS(id=uuid4(), name='沈青', profile_json='损坏的档案')]
    db = NS(execute=AsyncMock(side_effect=[empty, NS(scalars=lambda: NS(all=lambda: chars))]))
    pack = ContextPack()
    with pytest.raises(RequiredContextUnavailable, match='character_cards_context_unavailable'):
        await ContextPackBuilder(db=db)._build_facts(pack, pid, 2, global_chapter_idx=10)
    # The partially built local object exists, but the method must not return
    # it as a complete cast and proceed to generation without 沈青.
    assert [card.name for card in pack.character_cards] == ['陈川']


@pytest.mark.asyncio
async def test_character_profile_aliases_reach_real_card_assembly(monkeypatch):
    pid = uuid4()
    install_rule_graph(monkeypatch, pid)
    from app.services import character_roster
    class StopAfterAssembly(Exception): pass
    empty = NS(all=lambda: [])
    chars = [NS(id=uuid4(), name='陈川', profile_json={
        'aliases': [' 老陈 ', '老陈', None, 7, '', '陈川', '川'], 'location': '门外',
    })]
    db = NS(execute=AsyncMock(side_effect=[empty, NS(scalars=lambda: NS(all=lambda: chars))]))
    builder = ContextPackBuilder(db=db)
    monkeypatch.setattr(builder, '_enrich_characters_from_neo4j', AsyncMock())
    monkeypatch.setattr(builder, '_overlay_character_states', AsyncMock())
    monkeypatch.setattr(character_roster, 'load_historical_roster', AsyncMock(return_value=[]))
    monkeypatch.setattr(builder, '_cap_character_cards', AsyncMock(side_effect=StopAfterAssembly))
    pack = ContextPack()
    with pytest.raises(StopAfterAssembly):
        await builder._build_facts(pack, pid, 2, global_chapter_idx=10)
    assert pack.character_cards[0].aliases == ['老陈', '川']
    assert '老陈、川' in pack.character_cards[0].to_prompt()
