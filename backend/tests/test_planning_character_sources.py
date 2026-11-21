"""Real SQL/source checks around graph read doubles and retained material."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.project import Chapter, Volume
from app.services.entity_context import verified_world_snapshot
from app.services.entity_sources import resolve_entity_source, resolve_entity_sources
from app.services.planning_character_material import (
    PlanningCharacterMaterialError, character_source_material, render_character_source,
)
from app.services.planning_character_sources import read_planning_character_source
from tests.test_arc_character_material import character_book, character_material
from tests.support.arc_planning_runtime import planning_book


def receipt(source):
    return dict(source_chapter_id=source.chapter_id, source_hash=source.source_hash, global_idx=source.global_idx)


async def current(r, target=6):
    async with r.sessions() as db:
        return await read_planning_character_source(db, r.pid, target)


async def chapter(r, index, body, *, volume_id=None, pid=None, status='completed'):
    cid = uuid4()
    with r.engine.begin() as connection:
        connection.execute(sa.insert(Chapter).values(id=cid, volume_id=volume_id or r.vid,
            chapter_idx=index, global_idx=index, title=f'第{index}章', content_text=body, status=status))
    async with r.sessions() as db:
        source = await resolve_entity_source(db, pid or r.pid, 0, cid)
    return cid, source


@pytest.mark.asyncio
async def test_invalid_later_source_cannot_hide_an_earlier_location_with_its_end_date(character_book):
    r = character_book; await character_material(r)
    later_id, later = await chapter(r, 2, '陈川暂时上岸取药。')
    r.character_graph.locations[0]['chapter_end'] = 1
    r.character_graph.locations.append(dict(name='陈川', location='堤岸', chapter_end=None, **receipt(later)))
    with r.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == later_id).values(content_text='陈川并未离船。'))
    source = await current(r)
    assert [(row['location'], row['global_idx']) for row in source['locations']] == [('回程船舱', 1)]
    assert source['before_global_idx'] == 3


@pytest.mark.asyncio
async def test_same_chapter_locations_survive_without_inventing_intra_chapter_order(character_book):
    r = character_book; await character_material(r)
    other = dict(r.character_graph.locations[0], location='船头')
    r.character_graph.locations.extend([other, deepcopy(other)])
    source = await current(r)
    assert {row['location'] for row in source['locations']} == {'船头', '回程船舱'}
    assert len(source['locations']) == 2
    text = render_character_source(source, r.pid, 6)
    assert text.count('不能按列表顺序决定当前位置') == 2
    r.character_graph.locations.reverse()
    assert await current(r) == source


@pytest.mark.asyncio
async def test_locations_after_the_old_global_limit_are_not_lost(character_book):
    r = character_book; data = await character_material(r)
    r.character_graph.locations = [dict(name=f'登记者{i:04}', location=f'记录位置{i:04}',
        chapter_end=None, **receipt(data.source)) for i in range(2005)]
    source = await current(r)
    assert len(source['locations']) == 2005
    assert source['locations'][-1]['name'] == '登记者2004'
    assert '记录位置2004' in render_character_source(source, r.pid, 6)


@pytest.mark.asyncio
async def test_future_foreign_unfinished_and_mismatched_sources_never_enter_prior_locations(character_book):
    r = character_book; data = await character_material(r)
    later_id, later = await chapter(r, 2, '后章里的陈川已经到岸。')
    foreign_volume = uuid4()
    with r.engine.begin() as connection:
        connection.execute(sa.insert(Volume).values(id=foreign_volume, project_id=r.foreign, volume_idx=1, title='别书'))
    _, foreign = await chapter(r, 3, '另一本书的记录。', volume_id=foreign_volume, pid=r.foreign)
    r.character_graph.locations.extend([
        dict(name='未来人物', location='后章', **receipt(later)),
        dict(name='外书人物', location='外书', **dict(receipt(foreign), global_idx=1)),
        dict(name='坏摘要人物', location='旧稿', **dict(receipt(data.source), source_hash='0' * 64)),
        dict(name='未知编号人物', location='猜测', source_chapter_id='unknown', source_hash='0' * 64, global_idx=1),
        dict(name='位置错配人物', location='猜测', **dict(receipt(later), global_idx=1)),
    ])
    async with r.sessions() as db:
        snapshot = await verified_world_snapshot(db, r.character_graph, r.pid, 2, include_profiles=True)
    assert [row['name'] for row in snapshot.location_records] == ['陈川']
    with r.engine.begin() as connection:
        connection.execute(sa.update(Chapter).where(Chapter.id == later_id).values(status='draft'))
    assert [row['name'] for row in (await current(r))['locations']] == ['陈川']


@pytest.mark.asyncio
@pytest.mark.parametrize('origin,raw,identity', [(None, None, None), ('', '{}', ''), ('old_import', '{}', 'legacy-id')])
async def test_unknown_profile_origin_is_retained_without_fabricating_author_confirmation(character_book, origin, raw, identity):
    r = character_book; await character_material(r)
    row = r.character_graph.profiles[0]
    row.update(profile_origin=origin, profile_json=raw, id=identity)
    source = await current(r)
    assert source['profiles'] == [row]
    text = render_character_source(source, r.pid, 6)
    assert '登记档案，作者或提取出处未确认' in text
    assert '作者编辑的设定' not in text


@pytest.mark.asyncio
async def test_distinct_graph_nodes_with_the_same_name_cannot_merge_their_states(character_book):
    r = character_book; await character_material(r)
    r.character_graph.profiles.append(dict(r.character_graph.profiles[0], node_id='another-person-node',
        id=str(uuid4()), profile_json='{"身份":"另一名同名人物"}'))
    r.character_graph.states.append(dict(r.character_graph.states[0], status='{"另一人的资源":"药钱"}'))
    with pytest.raises(PlanningCharacterMaterialError): await current(r)
    assert len(r.character_graph.profiles) == 2 and len(r.character_graph.states) == 2


@pytest.mark.asyncio
async def test_shared_role_labels_and_aliases_keep_all_distinct_registered_people(character_book):
    r = character_book; await character_material(r)
    for name in ('沈远', '陈鸣'):
        r.character_graph.profiles.append(dict(node_id=f'node-{name}', id=str(uuid4()), name=name,
            profile_json=json.dumps({'称谓': '父亲', 'aliases': ['阿叔']}, ensure_ascii=False), profile_origin='author_edit'))
    source = await current(r)
    assert {row['name'] for row in source['profiles']} == {'林禾', '沈远', '陈鸣'}
    text = render_character_source(source, r.pid, 6)
    assert '沈远' in text and '陈鸣' in text and text.count('阿叔') == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['{"性格":"旧","性格":"新"}', '{"性格":{"倾向":1,"倾向":2}}', '{"数量":Infinity}'])
async def test_malformed_or_duplicate_profile_fields_are_not_silently_chosen(character_book, raw):
    r = character_book; await character_material(r)
    r.character_graph.profiles[0]['profile_json'] = raw
    with pytest.raises(PlanningCharacterMaterialError): await current(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['null', '[]', '{', '{"欠款":1000,"欠款":0}', '{"欠款":NaN}'])
async def test_malformed_current_state_is_not_reported_as_no_change(character_book, raw):
    r = character_book; await character_material(r)
    r.character_graph.states[0]['status'] = raw
    with pytest.raises(PlanningCharacterMaterialError): await current(r)


@pytest.mark.asyncio
async def test_same_source_conflicting_state_fields_require_review(character_book):
    r = character_book; await character_material(r)
    r.character_graph.states.append(dict(r.character_graph.states[0], status='{"欠款":0}'))
    with pytest.raises(PlanningCharacterMaterialError): await current(r)


@pytest.mark.asyncio
async def test_empty_later_delta_does_not_reattribute_earlier_state_fields(character_book):
    r = character_book; await character_material(r)
    _, later = await chapter(r, 2, '这一章未登记人物状态变化。')
    r.character_graph.states.append(dict(name='林禾', status='{}', **receipt(later)))
    source = await current(r)
    assert source['states'][0]['chapter_start'] == 1
    assert source['states'][0]['field_sources']['欠款']['global_idx'] == 1


@pytest.mark.asyncio
async def test_author_relationships_and_unresolved_prose_changes_retain_distinct_authority(character_book):
    r = character_book; await character_material(r)
    _, later = await chapter(r, 2, '陈川与林禾暂时停下送信的商量，先去找船主。')
    r.character_graph.relationships.append(dict(name='陈川', target='林禾', rel_type='共同找船主',
        raw_type='共同找船主，但旧委托的后续尚未说定', event_kind='change', replaces=None,
        evidence=later.content, **receipt(later)))
    r.character_graph.relationships.append(dict(name='林禾', target='陈川', rel_type='信任',
        raw_type='作者设定：林禾信任陈川办事的谨慎', origin='author', author_id=str(uuid4()),
        author_edit_id=str(uuid4()), author_updated_at=1, note='这不是已经发生的承诺。'))
    source = await current(r)
    text = render_character_source(source, r.pid, 6)
    assert '[作者设定] 林禾 → 陈川' in text
    assert 'CH-2发生变化后，本项是否仍成立待核对' in text
    assert later.content in text and '未承诺代偿' in text
    r.character_graph.relationships.reverse()
    assert await current(r) == source


@pytest.mark.asyncio
async def test_empty_book_uses_real_first_positions_without_persisting_a_fake_chapter(character_book):
    r = character_book
    with r.engine.begin() as connection:
        connection.execute(sa.delete(Chapter).where(Chapter.id == r.cid))
        connection.execute(sa.delete(Volume).where(Volume.id == r.vid))
    source = await current(r, target=1)
    assert source['before_global_idx'] == 1 and source['target_volume_idx'] == 1
    assert source['volume_order'] == source['chapter_order'] == []
    with Session(r.engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Chapter)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['unknown', 'zero_global', 'negative_global', 'oversize_global', 'zero_local',
    'duplicate_global', 'duplicate_local', 'backwards_global', 'backwards_volume', 'unknown_status', 'wrong_target'])
async def test_invalid_persisted_order_does_not_invent_a_planning_boundary(character_book, case):
    r = character_book
    with r.engine.begin() as connection:
        values = {'unknown': {'global_idx': None}, 'zero_global': {'global_idx': 0},
            'negative_global': {'global_idx': -1}, 'oversize_global': {'global_idx': 2147483647},
            'zero_local': {'chapter_idx': 0}, 'unknown_status': {'status': ''}}.get(case)
        if values: connection.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(**values))
        if case in {'duplicate_global', 'duplicate_local', 'backwards_global', 'backwards_volume'}:
            vid = r.vid
            if case == 'backwards_volume':
                vid = uuid4()
                connection.execute(sa.insert(Volume).values(id=vid, project_id=r.pid, volume_idx=4, title='前卷'))
            connection.execute(sa.insert(Chapter).values(id=uuid4(), volume_id=vid,
                chapter_idx=1 if case == 'duplicate_local' else 2,
                global_idx=1 if case == 'duplicate_global' else 2, title='顺序边界用例', status='draft'))
            if case == 'backwards_global':
                connection.execute(sa.update(Chapter).where(Chapter.id == r.cid).values(global_idx=3))
    with pytest.raises(PlanningCharacterMaterialError): await current(r, target=7 if case == 'wrong_target' else 6)
    assert not r.character_graph.calls, 'bad chapter layout reached graph reads'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['state_end', 'state_start', 'state_field_source', 'state_duplicate',
    'relationship_end', 'relationship_identity', 'relationship_scope', 'relationship_origin',
    'location_source', 'profile_identity', 'boundary', 'foreign_volume'])
async def test_retained_material_revalidates_boundaries_and_receipts(character_book, case):
    r = character_book; await character_material(r)
    source = await current(r)
    if case == 'state_end': source['states'][0]['chapter_end'] = 1
    elif case == 'state_start': source['states'][0]['chapter_start'] = 2
    elif case == 'state_field_source': source['states'][0]['field_sources']['欠款']['source_chapter_id'] = str(uuid4())
    elif case == 'state_duplicate': source['states'].append(deepcopy(source['states'][0]))
    elif case == 'relationship_end': source['relationships'][0]['chapter_end'] = 1
    elif case == 'relationship_identity': source['relationships'][0]['record_id'] = 'rel_' + '0' * 64
    elif case == 'relationship_scope': source['relationships'][0]['scope_pending'] = {'global_idx': 900}
    elif case == 'relationship_origin': source['relationships'][0]['origin'] = 'author'
    elif case == 'location_source': source['locations'][0]['source_chapter_id'] = str(uuid4())
    elif case == 'profile_identity': source['profiles'].append(deepcopy(source['profiles'][0]))
    elif case == 'boundary': source['before_global_idx'] = 900
    else: source['chapter_order'][0]['volume_id'] = str(uuid4())
    with pytest.raises(PlanningCharacterMaterialError): character_source_material(source, r.pid, 6)


@pytest.mark.asyncio
async def test_five_million_characters_are_verified_in_bounded_actual_body_batches(character_book):
    r = character_book; data = await character_material(r)
    body = '船舱中的借据尚未核完，林禾仍须按实际进项还款。' * 240
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i, global_idx=i, title=f'第{i}章',
                 content_text=body, status='completed') for i in range(2, 1007)]
    with r.engine.begin() as connection: connection.execute(sa.insert(Chapter), rows)
    async with r.sessions() as db:
        sources = await resolve_entity_sources(db, r.pid, [row['id'] for row in rows])
    for source in sources.values():
        r.character_graph.states.append(dict(name='林禾', status=json.dumps({'账目核对进度': f'核至第{source.global_idx}章'}),
                                             **receipt(source)))
    total = len(data.body) + len(body) * len(rows)
    assert total >= 5_000_000
    reads = []
    async with r.sessions() as db:
        class Reader:
            async def execute(self, statement):
                result = await db.execute(statement)
                if any(column['name'] == 'content_text' for column in statement.column_descriptions):
                    values = result.all()
                    reads.append((len(values), sum(len(row.content_text) for row in values)))
                    return NS(all=lambda: values)
                return result
        first = await read_planning_character_source(Reader(), r.pid, 6)
        first_reads = list(reads); reads.clear()
        second = await read_planning_character_source(Reader(), r.pid, 6)
    assert second == first
    assert sum(size for _, size in first_reads) == sum(size for _, size in reads) == total
    assert len(first_reads) == len(reads) == 6 and max(count for count, _ in reads) <= 200
    assert len(first['chapter_order']) == 1006
    state = first['states'][0]
    assert state['status']['左手'] == '暂不能握紧绳索' and state['status']['账目核对进度'] == '核至第1006章'
    assert state['field_sources']['左手']['global_idx'] == 1
    assert state['field_sources']['账目核对进度']['global_idx'] == 1006
    assert all('content_text' not in row for row in first['chapter_order'])
