"""Exercise source reads and the actual character prompt without live services.

SQLite verifies selection, source receipts and rendering. It does not prove
PostgreSQL locking or Neo4j availability; those keep their integration tests.
"""
from dataclasses import asdict
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Uuid, create_engine, delete, insert, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.project import (
    Project, Volume, Chapter, Character, CharacterState, CharacterLocation,
    Location, WorldRule,
)
from app.services.context_pack import CharacterCard, ContextPack, ContextPackBuilder
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import resolve_entity_source
from app.services.entity_timeline import CharacterSnapshot, WorldSnapshot
from tests.support.world_rules import install_rule_graph


@pytest.fixture
def state_db(monkeypatch):
    engine = create_engine('sqlite://')
    for model in (Project, Volume, Chapter, Character, CharacterState,
                  Location, CharacterLocation, WorldRule):
        model.__table__.create(engine)
    with Session(engine) as session:
        pid, vid, char_id = uuid4(), uuid4(), uuid4()
        install_rule_graph(monkeypatch, pid)
        session.execute(insert(Project).values(id=pid, title='状态上下文测试'))
        session.execute(insert(Volume).values(id=vid, project_id=pid, volume_idx=2, title='归来'))
        session.execute(insert(Character).values(id=char_id, project_id=pid, name='陈川',
            profile_json={'location': '家中', 'power_level': '凡人', 'mental_state': '平静'}))
        session.commit()
        queries = []

        async def execute(statement):
            # asyncpg accepts UUID strings; SQLite's UUID bind requires UUID
            # objects. Adapt only that representation, executing the real SQL.
            compiled = statement.compile()
            parameters = {}
            for key, value in compiled.params.items():
                if isinstance(compiled.binds[key].type, Uuid):
                    def convert(item):
                        return UUID(item) if isinstance(item, str) else item
                    parameters[key] = [convert(item) for item in value] if isinstance(value, (list, tuple)) else convert(value)
            queries.append(statement)
            return session.execute(statement, parameters)

        yield NS(session=session, db=NS(execute=execute), pid=pid, vid=vid,
                 character_id=char_id, queries=queries)
    engine.dispose()


async def add_state(r, chapter, status, *, body='陈川左手骨折，身上一文钱也没有。他仍保持镇定。'):
    cid = uuid4()
    r.session.execute(insert(Chapter).values(id=cid, volume_id=r.vid, chapter_idx=chapter - 899,
        global_idx=chapter, title=f'第{chapter}章', content_text=body, status='completed'))
    r.session.commit()
    source = await resolve_entity_source(r.db, r.pid, 0, cid)
    r.session.execute(insert(CharacterState).values(project_id=r.pid, character_id=r.character_id,
        chapter_start=chapter, status_json=status, source_mode='chapter_delta',
        source_chapter_id=cid, source_hash=source.source_hash))
    r.session.commit()
    return source


async def overlay(r, *, before=902, card=None):
    pack = ContextPack(character_cards=[card or CharacterCard(name='陈川')])
    await ContextPackBuilder(db=r.db)._overlay_character_states(pack, str(r.pid), before)
    return pack


def receipt(source):
    return dict(source_chapter_id=source.chapter_id, source_hash=source.source_hash,
                global_idx=source.global_idx)


def state_lines(prompt):
    return [line for line in prompt.splitlines() if line.startswith('  正文状态[CH-')]


async def assemble_cards(r, monkeypatch, *, before=902):
    """Run the production fact builder through the real SQL/card assembly."""
    class CardsBuilt(Exception):
        pass

    builder = ContextPackBuilder(db=r.db)
    monkeypatch.setattr(builder, '_enrich_characters_from_neo4j', AsyncMock())
    monkeypatch.setattr('app.services.character_roster.load_historical_roster', AsyncMock(return_value=[]))
    monkeypatch.setattr(builder, '_cap_character_cards', AsyncMock(side_effect=CardsBuilt))
    pack = ContextPack(history_before_global_idx=before)
    with pytest.raises(CardsBuilt):
        await builder._build_facts(pack, r.pid, 3, before)
    return pack


@pytest.mark.asyncio
async def test_body_states_survive_actual_overlay_and_required_prompt(state_db):
    r = state_db
    await add_state(r, 900, {'伤势': '左手骨折', '身份': '停职巡检',
        '资源': {'银两': 0, '凭据': []}, '情绪': '镇定'})
    await add_state(r, 901, {'情绪': '略安'}, body='听见沈青带药回来，陈川才略略放下心。')
    pack = await overlay(r)
    prompt = pack.to_system_prompt(token_budget=1800)
    assert all(value in prompt for value in ('左手骨折', '停职巡检', '银两', '凭据', '略安'))
    assert '[CH-900]' in prompt and '[CH-901]' in prompt
    assert '镇定' not in prompt
    assert next(row for row in pack.budget_trace if row['source'] == 'character_cards')['required']


@pytest.mark.asyncio
async def test_general_status_and_emotion_remain_distinct(state_db):
    await add_state(state_db, 900, {'状态': '重伤', '情绪': '镇定'})
    pack = await overlay(state_db)
    prompt = pack.to_system_prompt()
    assert '重伤' in prompt and '镇定' in prompt
    assert pack.character_cards[0].mental_state == '镇定'


@pytest.mark.asyncio
async def test_latest_supported_emotion_alias_is_used(state_db):
    await add_state(state_db, 900, {'mental_state': '焦虑'})
    await add_state(state_db, 901, {'情绪': '释然'}, body='听见旧友平安的消息，陈川终于释然。')
    pack = await overlay(state_db)
    assert pack.character_cards[0].mental_state == '释然'


@pytest.mark.asyncio
async def test_required_state_read_error_is_not_an_empty_history(monkeypatch):
    error = RuntimeError('private storage failure')
    monkeypatch.setattr('app.services.entity_context.verified_character_states', AsyncMock(side_effect=error))
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', mental_state='旧档案值')])
    with pytest.raises(RequiredContextUnavailable, match='character_states_context_unavailable') as caught:
        await ContextPackBuilder(db=NS())._overlay_character_states(pack, str(uuid4()), 902)
    assert caught.value.__cause__ is error


@pytest.mark.asyncio
async def test_null_false_zero_and_empty_collections_are_not_missing_fields(state_db):
    r = state_db
    before = {'伤势': '旧伤势', '可战': True, '灵力': 100, '装备': ['旧剑'], '携带': {'钱': 3}, '口令': '旧口令'}
    after = {'伤势': None, '可战': False, '灵力': 0, '装备': [], '携带': {}, '口令': ''}
    await add_state(r, 900, before)
    await add_state(r, 901, after, body='陈川交出剑和行囊，最后一点灵力也耗尽了。')
    pack = await overlay(r)
    lines = state_lines(pack.to_system_prompt())
    assert len(lines) == 1 and '[CH-901]' in lines[0]
    assert json.loads(lines[0].split('：', 1)[1]) == after
    assert pack.character_cards[0].chapter_state == after


@pytest.mark.asyncio
async def test_general_state_alone_is_never_relabelled_as_psychology(state_db):
    await add_state(state_db, 900, {'状态': '重伤'})
    pack = await overlay(state_db, card=CharacterCard(name='陈川', mental_state='镇定'))
    prompt = pack.to_system_prompt()
    assert '重伤' in prompt and '档案心理:镇定' in prompt
    assert '心理:重伤' not in prompt


@pytest.mark.asyncio
async def test_new_unknown_alias_does_not_restore_profile_or_old_prose_values(state_db):
    await add_state(state_db, 900, {'power_level': '炼气', 'mental_state': '焦虑', 'location': '旧宅'})
    await add_state(state_db, 901, {'能力等级': None, '情绪': None, '位置': None})
    pack = await overlay(state_db, card=CharacterCard(name='陈川', location='家中',
        power_level='凡人', mental_state='平静'))
    card = pack.character_cards[0]
    assert (card.power_level, card.mental_state, card.location) == ('', '', '')
    lines = state_lines(pack.to_system_prompt())
    assert len(lines) == 2  # Older aliases retain their dates, not timeless authority.
    assert json.loads(lines[-1].split('：', 1)[1]) == {'能力等级': None, '情绪': None, '位置': None}
    assert all(value not in pack.to_system_prompt() for value in ('家中', '凡人', '平静'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['body', 'draft', 'delete', 'reindex', 'hash', 'legacy', 'foreign'])
async def test_invalidated_older_source_cannot_be_retained_as_complete_state(state_db, mutation):
    r = state_db
    old = await add_state(r, 900, {'伤势': '左手骨折'})
    await add_state(r, 901, {'情绪': '略安'})
    old_id = UUID(old.chapter_id)
    if mutation == 'body':
        r.session.execute(update(Chapter).where(Chapter.id == old_id).values(content_text='作者已改写这场冲突。'))
    elif mutation == 'draft':
        r.session.execute(update(Chapter).where(Chapter.id == old_id).values(status='draft'))
    elif mutation == 'delete':
        r.session.execute(delete(Chapter).where(Chapter.id == old_id))
    elif mutation == 'reindex':
        r.session.execute(update(Chapter).where(Chapter.id == old_id).values(global_idx=910))
    elif mutation == 'hash':
        r.session.execute(update(CharacterState).where(CharacterState.source_chapter_id == old_id).values(source_hash='bad'))
    elif mutation == 'legacy':
        r.session.execute(update(CharacterState).where(CharacterState.source_chapter_id == old_id).values(source_mode=None))
    else:
        other_pid, other_vid = uuid4(), uuid4()
        r.session.execute(insert(Project).values(id=other_pid, title='别的书'))
        r.session.execute(insert(Volume).values(id=other_vid, project_id=other_pid, volume_idx=1, title='异书'))
        r.session.execute(update(Chapter).where(Chapter.id == old_id).values(volume_id=other_vid))
    r.session.commit()
    pack = await overlay(r)
    assert pack.character_cards[0].chapter_state == {'情绪': '略安'}
    assert '左手骨折' not in pack.to_system_prompt()


@pytest.mark.asyncio
async def test_current_and_future_states_never_enter_earlier_chapter(state_db):
    await add_state(state_db, 900, {'伤势': '左手骨折'})
    await add_state(state_db, 901, {'伤势': '已治愈'})
    await add_state(state_db, 902, {'身份': '未来任命'})
    pack = await overlay(state_db, before=901)
    assert pack.character_cards[0].chapter_state == {'伤势': '左手骨折'}
    assert all(value not in pack.to_system_prompt() for value in ('已治愈', '未来任命'))


@pytest.mark.asyncio
async def test_full_assembly_preserves_raw_fields_and_labels_profile_references(state_db, monkeypatch):
    source = await add_state(state_db, 900, {'伤势': '左手骨折', '资源': {'银两': 0}, '情绪': '镇定'})
    pack = await assemble_cards(state_db, monkeypatch)
    card = pack.character_cards[0]
    prompt = pack.to_system_prompt()
    assert '左手骨折' in prompt and '银两' in prompt
    assert '档案位置:家中' in prompt and '档案实力:凡人' in prompt
    assert '档案心理:平静' not in prompt
    assert card.chapter_state_sources['伤势'] == receipt(source)


@pytest.mark.asyncio
@pytest.mark.parametrize('location_chapter,state_chapter,state_value,expected', [
    (900, 901, '谷口', '谷口'), (901, 900, '谷口', '客栈'), (900, 901, None, ''),
])
async def test_location_projection_and_state_use_source_chronology(
    state_db, monkeypatch, location_chapter, state_chapter, state_value, expected,
):
    r = state_db
    source = await add_state(r, location_chapter, {'情绪': '镇定'})
    await add_state(r, state_chapter, {'位置': state_value})
    lid = uuid4()
    r.session.execute(insert(Location).values(id=lid, project_id=r.pid, name='客栈'))
    r.session.execute(insert(CharacterLocation).values(project_id=r.pid, character_id=r.character_id,
        location_id=lid, chapter_start=location_chapter, source_chapter_id=UUID(source.chapter_id),
        source_hash=source.source_hash))
    r.session.commit()
    pack = await assemble_cards(r, monkeypatch)
    card = pack.character_cards[0]
    assert card.location == expected
    assert card.location_source['global_idx'] == max(location_chapter, state_chapter)
    assert '家中' not in card.to_prompt()
    assert '档案位置' not in card.to_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('state_location', ['门外', None])
async def test_same_chapter_location_record_is_not_deduplicated_against_different_state(
    state_db, monkeypatch, state_location,
):
    r = state_db
    source = await add_state(r, 901, {'位置': state_location},
                             body='陈川在门外等了一会儿，随后进了客栈。')
    lid = uuid4()
    r.session.execute(insert(Location).values(id=lid, project_id=r.pid, name='客栈'))
    r.session.execute(insert(CharacterLocation).values(project_id=r.pid, character_id=r.character_id,
        location_id=lid, chapter_start=901, source_chapter_id=UUID(source.chapter_id),
        source_hash=source.source_hash))
    r.session.commit()
    pack = await assemble_cards(r, monkeypatch)
    assert pack.character_cards[0].location == ''
    prompt = pack.to_system_prompt()
    assert '位置[CH-901]:客栈' in prompt
    assert '不能按列表顺序决定当前位置' in prompt
    assert '同章位置以此记录为准' not in prompt
    assert json.loads(state_lines(prompt)[0].split('：', 1)[1]) == {'位置': state_location}


def test_replacing_a_state_location_with_null_cannot_leave_a_stale_shortcut():
    source = dict(global_idx=901, source_chapter_id=str(uuid4()), source_hash='verified-source')
    card = CharacterCard(name='陈川', location='档案位置')
    card.apply_chapter_state({'位置': '客栈'}, {'位置': source})
    card.apply_chapter_state({'位置': None}, {'位置': source})
    assert card.location == ''
    assert '客栈' not in card.to_prompt() and '档案位置' not in card.to_prompt()
    assert '"位置": null' in card.to_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('private location failure'), SQLAlchemyError('private DB failure')])
async def test_location_read_error_stops_actual_fact_builder(monkeypatch, error):
    pid = uuid4()
    install_rule_graph(monkeypatch, pid)
    db = NS(execute=AsyncMock(side_effect=error))
    expected = SQLAlchemyError if isinstance(error, SQLAlchemyError) else RequiredContextUnavailable
    with pytest.raises(expected) as caught:
        await ContextPackBuilder(db=db)._build_facts(ContextPack(), pid, 3, 902)
    if expected is RequiredContextUnavailable:
        assert str(caught.value) == 'character_locations_context_unavailable'
        assert caught.value.__cause__ is error
    assert db.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('private cognition failure'), SQLAlchemyError('private DB failure')])
async def test_cognition_read_error_stops_actual_fact_builder(monkeypatch, error):
    pid = uuid4()
    install_rule_graph(monkeypatch, pid)
    empty = NS(all=lambda: [], scalars=lambda: NS(all=lambda: []))
    db = NS(execute=AsyncMock(return_value=empty))
    builder = ContextPackBuilder(db=db)
    monkeypatch.setattr(builder, '_enrich_characters_from_neo4j', AsyncMock())
    monkeypatch.setattr('app.services.character_roster.load_historical_roster', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(side_effect=error))
    expected = SQLAlchemyError if isinstance(error, SQLAlchemyError) else RequiredContextUnavailable
    with pytest.raises(expected) as caught:
        await builder._build_facts(ContextPack(history_before_global_idx=902), pid, 3, 902)
    if expected is RequiredContextUnavailable:
        assert str(caught.value) == 'character_cognition_context_unavailable'
        assert caught.value.__cause__ is error


def mock_graph(monkeypatch, snapshot):
    async def driver():
        yield object()
    monkeypatch.setattr('app.db.neo4j.get_neo4j', driver)
    monkeypatch.setattr('app.services.entity_context.verified_world_snapshot', AsyncMock(return_value=snapshot))


@pytest.mark.asyncio
async def test_graph_enrichment_retains_newer_fields_when_pg_projection_is_older(state_db, monkeypatch):
    r = state_db
    await add_state(r, 900, {'状态': '无伤', '能力等级': '炼气', '情绪': '焦虑'})
    source = await add_state(r, 901, {'状态': '左手骨折', '能力等级': '筑基'})
    r.session.execute(delete(CharacterState).where(CharacterState.chapter_start == 901))
    r.session.commit()
    status = {'状态': '左手骨折', '能力等级': '筑基'}
    snapshot = WorldSnapshot(characters=[CharacterSnapshot('陈川', status, 901,
        field_sources={key: receipt(source) for key in status})])
    mock_graph(monkeypatch, snapshot)
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', power_level='凡人')])
    builder = ContextPackBuilder(db=r.db)
    await builder._enrich_characters_from_neo4j(pack, str(r.pid), 902)
    await builder._overlay_character_states(pack, str(r.pid), 902)
    assert pack.character_cards[0].power_level == '筑基'
    assert pack.character_cards[0].chapter_state == {**status, '情绪': '焦虑'}
    assert '无伤' not in pack.to_system_prompt()
    assert '[CH-900]' in pack.to_system_prompt() and '[CH-901]' in pack.to_system_prompt()


@pytest.mark.asyncio
async def test_graph_only_character_also_retains_complete_dated_state(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    status = {'伤势': '左肩中箭', '装备': ['断弓']}
    mock_graph(monkeypatch, WorldSnapshot(characters=[CharacterSnapshot('沈青', status, 900,
        field_sources={key: receipt(source) for key in status})]))
    pack = ContextPack()
    await ContextPackBuilder(db=state_db.db)._enrich_characters_from_neo4j(pack, str(state_db.pid), 902)
    assert pack.character_cards[0].chapter_state == status
    assert all(value in pack.to_system_prompt() for value in ('沈青', '左肩中箭', '断弓', '[CH-900]'))


@pytest.mark.asyncio
async def test_malformed_required_graph_context_never_leaves_half_a_cast(state_db, monkeypatch):
    source = await add_state(state_db, 900, {})
    mock_graph(monkeypatch, WorldSnapshot(characters=[
        CharacterSnapshot('陈川', {'伤势': '左手骨折'}, 900, field_sources={'伤势': receipt(source)}),
        CharacterSnapshot('沈青', {'状态': '重伤'}, 900),  # Missing per-field provenance.
    ]))
    pack = ContextPack(character_cards=[CharacterCard(name='陈川', power_level='凡人')])
    original = asdict(pack.character_cards[0])
    with pytest.raises(RequiredContextUnavailable, match='character_graph_context_unavailable'):
        await ContextPackBuilder(db=state_db.db)._enrich_characters_from_neo4j(pack, str(state_db.pid), 902)
    assert [asdict(card) for card in pack.character_cards] == [original]


@pytest.mark.asyncio
async def test_arbitrary_required_state_fields_are_not_shortened_to_fit_budget(state_db):
    from app.services.context_budget import ContextBudgetExceeded
    await add_state(state_db, 900, {'伤势': '行动限制与伤情记录。' * 120, '物品': ['唯一凭据']})
    pack = await overlay(state_db)
    with pytest.raises(ContextBudgetExceeded, match='character_cards'):
        pack.to_system_prompt(token_budget=500)
    prompt = pack.to_system_prompt(token_budget=2500)
    assert '行动限制与伤情记录。' * 120 in prompt and '唯一凭据' in prompt


def test_dated_cognition_prompt_does_not_treat_an_excerpt_as_total_knowledge():
    from app.services.character_cognition import serialize_for_prompt
    history = {'陈川': {'history': [
        {'chapter_idx': 900, 'kind': 'still_unknown', 'fact': '送药人身份'},
        {'chapter_idx': 901, 'kind': 'learns', 'fact': '送药人正是沈青'},
    ]}}
    prompt = ContextPack(cognition_boundaries=serialize_for_prompt(history)).to_system_prompt()
    assert '节选不是人物的全部知识' in prompt
    assert '只能依据其「知道」列表行动' not in prompt
    assert '后章明确获知同一信息后不能再据旧记录断言其仍未知' in prompt
    assert '[CH-900]' in prompt and '[CH-901]' in prompt


@pytest.mark.asyncio
async def test_actual_fact_builder_keeps_selected_character_cognition_evidence(monkeypatch):
    pid = uuid4()
    install_rule_graph(monkeypatch, pid)
    empty = NS(all=lambda: [], scalars=lambda: NS(all=lambda: []), first=lambda: None)
    db = NS(execute=AsyncMock(return_value=empty))
    builder = ContextPackBuilder(db=db)
    for name in ('_enrich_characters_from_neo4j', '_build_returning_drilldown', '_build_strand_tracker'):
        monkeypatch.setattr(builder, name, AsyncMock())
    monkeypatch.setattr('app.services.character_roster.load_historical_roster', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.related_chapters.find_related_chapters', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.compass_service.load_compass', AsyncMock(return_value=None))
    ledger = {'先生': {'history': [dict(chapter_idx=1, kind='learns', fact='信送到了。', evidence='先生读到了回信。')]}}
    ledger.update({f'旁人{i}': {'history': [dict(chapter_idx=i + 2, kind='learns', fact='另条支线的消息。', evidence='另条支线的消息。')]} for i in range(100)})
    from app.services.cognition_validation import CognitionLedger
    from app.services.entity_sources import digest
    from app.services import cognition_retrieval as retrieval
    from tests.test_cognition_retrieval import FixedSpace
    from qdrant_client import AsyncQdrantClient
    from contextlib import asynccontextmanager
    ledger = CognitionLedger(ledger)
    ledger.snapshot_hash = digest(ledger)
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value=ledger))
    target_id = str(uuid4())
    pack = ContextPack(history_before_global_idx=902, target_chapter_id=target_id,
                       character_cards=[CharacterCard(name='陈川', aliases=['先生'])])
    await builder._build_facts(pack, pid, 3, 902)
    assert isinstance(pack.cognition_boundaries, retrieval.CognitionPrompt)
    assert pack.cognition_boundaries.before_chapter_id == target_id
    @asynccontextmanager
    async def sessions(): yield db
    @asynccontextmanager
    async def index_lock(project_id, *, wait=False): yield NS(check=AsyncMock())
    resolve = retrieval.resolve_prompt_context
    client = AsyncQdrantClient(':memory:')
    async def resolve_locally(value, **kwargs):
        return await resolve(value, client=client, sessions=sessions,
                             space_loader=AsyncMock(return_value=FixedSpace()), lock_factory=index_lock, **kwargs)
    monkeypatch.setattr(retrieval, 'resolve_prompt_context', resolve_locally)
    try: await pack.resolve_cognition()
    finally: await client.close()
    prompt = pack.to_system_prompt()
    assert '先生读到了回信。' in prompt and '先生·本章认知记录' in prompt
    assert '陈川·本章认知记录' not in prompt
