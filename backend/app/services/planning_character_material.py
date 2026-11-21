"""Reviewable character records, with settings separate from sourced prose."""
from collections import Counter
from dataclasses import fields
import json
import re
from uuid import UUID

from app.services.character_context import CharacterCard
from app.services.entity_timeline import RelationshipSnapshot
from app.services.relationship_context import render_relationship_record
from app.services.relationship_identity import relationship_record_id, valid_replacement_ids


class PlanningCharacterMaterialError(ValueError):
    pass


class PlanningCharacterUnavailable(RuntimeError):
    pass


def _identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('invalid identity')


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('invalid text')


def _index(value):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise ValueError('invalid position')


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('duplicate profile field')
        result[key] = value
    return result


def _profile(row):
    if not isinstance(row, dict) or set(row) != {'node_id', 'id', 'name', 'profile_json', 'profile_origin'}:
        raise ValueError('invalid profile record')
    _text(row['node_id']); _text(row['name'])
    # Historical nodes may have an empty ID or unknown origin. Retain them
    # verbatim; the real Neo4j node identity still detects delete/recreate.
    for key in ('id', 'profile_origin'):
        if row[key] is not None and not isinstance(row[key], str):
            raise ValueError('invalid profile metadata')
    raw = row['profile_json']
    if raw is None:
        profile = {}
    elif isinstance(raw, str):
        profile = json.loads(raw, object_pairs_hook=_pairs)
    else:
        raise ValueError('invalid graph profile')
    if not isinstance(profile, dict):
        raise ValueError('invalid graph profile')
    json.dumps(profile, allow_nan=False)
    return profile


def character_source_material(source, project_id, target_volume_idx):
    """Validate the retained shape; actual body verification belongs to the reader."""
    try:
        if (not isinstance(source, dict) or set(source) != {'version', 'project_id', 'target_volume_idx',
                'before_global_idx', 'volume_order', 'chapter_order', 'profiles', 'states', 'relationships', 'locations'}
                or type(source['version']) is not int or source['version'] != 1
                or source['project_id'] != str(project_id) or source['target_volume_idx'] != target_volume_idx):
            raise ValueError('invalid character material')
        _identity(source['project_id']); _index(source['target_volume_idx']); _index(source['before_global_idx'])
        volumes, positions = {}, {}
        if not isinstance(source['volume_order'], list) or not isinstance(source['chapter_order'], list):
            raise ValueError('invalid order')
        for row in source['volume_order']:
            if not isinstance(row, dict) or set(row) != {'id', 'volume_idx'}: raise ValueError('invalid volume')
            _identity(row['id']); _index(row['volume_idx'])
            if row['id'] in volumes: raise ValueError('duplicate volume')
            volumes[row['id']] = row['volume_idx']
        if (len(set(volumes.values())) != len(volumes)
                or source['target_volume_idx'] != max(volumes.values(), default=0) + 1):
            raise ValueError('invalid tail volume')
        local_positions, global_positions, ordered = set(), set(), []
        for row in source['chapter_order']:
            if not isinstance(row, dict) or set(row) != {'id', 'volume_id', 'chapter_idx', 'global_idx', 'status'}:
                raise ValueError('invalid chapter')
            _identity(row['id']); _identity(row['volume_id']); _index(row['chapter_idx']); _index(row['global_idx'])
            _text(row['status'])
            pair = (row['volume_id'], row['chapter_idx'])
            if row['id'] in positions or row['volume_id'] not in volumes or pair in local_positions or row['global_idx'] in global_positions:
                raise ValueError('ambiguous chapter')
            positions[row['id']] = row
            local_positions.add(pair); global_positions.add(row['global_idx'])
            ordered.append((volumes[row['volume_id']], row['chapter_idx'], row['global_idx']))
        if ([row[2] for row in sorted(ordered)] != sorted(global_positions)
                or source['before_global_idx'] != max(global_positions, default=0) + 1):
            raise ValueError('ambiguous global boundary')

        def reference(row):
            _identity(row['source_chapter_id']); _index(row['global_idx'])
            if not isinstance(row['source_hash'], str) or not re.fullmatch('[0-9a-f]{64}', row['source_hash']):
                raise ValueError('invalid source hash')
            position = positions.get(row['source_chapter_id'])
            if not position or position['global_idx'] != row['global_idx'] or position['status'] != 'completed':
                raise ValueError('invalid prior prose reference')

        if any(not isinstance(source[key], list) for key in ('profiles', 'states', 'relationships', 'locations')):
            raise ValueError('invalid character records')
        node_ids, profile_names = set(), set()
        for row in source['profiles']:
            _profile(row)
            if row['node_id'] in node_ids or row['name'] in profile_names:
                raise ValueError('ambiguous graph character identity')
            node_ids.add(row['node_id'])
            profile_names.add(row['name'])
        cards, state_names = [], set()
        for row in source['states']:
            if not isinstance(row, dict) or set(row) != {'name', 'status', 'chapter_start', 'chapter_end', 'field_sources'}:
                raise ValueError('invalid state')
            _text(row['name']); _index(row['chapter_start'])
            if (row['chapter_end'] is not None or row['name'] in state_names
                    or not isinstance(row['status'], dict) or not row['status']
                    or not isinstance(row['field_sources'], dict)
                    or set(row['status']) != set(row['field_sources'])):
                raise ValueError('incomplete state sources')
            state_names.add(row['name'])
            for receipt in row['field_sources'].values():
                if not isinstance(receipt, dict) or set(receipt) != {'source_chapter_id', 'source_hash', 'global_idx'}:
                    raise ValueError('invalid state source')
                reference(receipt)
            if row['chapter_start'] != max(receipt['global_idx'] for receipt in row['field_sources'].values()):
                raise ValueError('invalid state boundary')
            card = CharacterCard(name=row['name'])
            card.apply_chapter_state(row['status'], row['field_sources'])
            cards.append(card)
        relationships = []
        for row in source['relationships']:
            if not isinstance(row, dict) or set(row) != {item.name for item in fields(RelationshipSnapshot)}:
                raise ValueError('invalid relationship')
            rel = RelationshipSnapshot(**row)
            _text(rel.source); _text(rel.target); _text(rel.rel_type)
            if rel.chapter_end is not None or any(not isinstance(getattr(rel, key), str)
                    for key in ('raw_type', 'label', 'note', 'sentiment', 'evidence')):
                raise ValueError('invalid relationship metadata')
            if ((rel.replaces is not None and not valid_replacement_ids(rel.replaces))
                    or not valid_replacement_ids(rel.unresolved_replaces)
                    or not set(rel.unresolved_replaces).issubset(rel.replaces or [])):
                raise ValueError('invalid relationship scope')
            if rel.origin == 'prose':
                reference(dict(source_chapter_id=rel.source_chapter_id, source_hash=rel.source_hash, global_idx=rel.chapter_start))
                if (any(getattr(rel, key) is not None for key in ('author_id', 'author_edit_id', 'author_updated_at'))
                        or rel.record_id != relationship_record_id(project_id, rel.source, rel.target, rel.rel_type,
                            rel.source_chapter_id, rel.source_hash, rel.raw_type)):
                    raise ValueError('invalid relationship identity')
                if rel.scope_pending is not None:
                    scope = rel.scope_pending
                    if (not isinstance(scope, dict) or set(scope) != {'global_idx', 'source_chapter_id',
                            'source_hash', 'record_id', 'rel_type', 'evidence'}):
                        raise ValueError('invalid pending scope')
                    reference(scope); _text(scope['rel_type'])
                    if (not valid_replacement_ids([scope['record_id']]) or not isinstance(scope['evidence'], str)
                            or scope['global_idx'] <= rel.chapter_start):
                        raise ValueError('invalid pending scope source')
            elif rel.origin == 'author':
                if (type(rel.chapter_start) is not int or rel.chapter_start != 0
                        or any(getattr(rel, key) is not None for key in ('source_chapter_id', 'source_hash',
                            'event_kind', 'record_id', 'replaces', 'scope_pending'))
                        or rel.unresolved_replaces or rel.evidence):
                    raise ValueError('author setting presented as prose')
                _identity(rel.author_id); _identity(rel.author_edit_id)
            else:
                raise ValueError('unknown relationship authority')
            relationships.append(render_relationship_record(rel))
        location_positions, location_keys = {}, set()
        for row in source['locations']:
            if not isinstance(row, dict) or set(row) != {'name', 'location', 'global_idx', 'source_chapter_id', 'source_hash'}:
                raise ValueError('invalid location')
            _text(row['name']); _text(row['location']); reference(row)
            key = (row['name'], row['location'])
            if key in location_keys or location_positions.get(row['name'], row['global_idx']) != row['global_idx']:
                raise ValueError('invalid current location boundary')
            location_keys.add(key); location_positions[row['name']] = row['global_idx']
        json.dumps(source, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCharacterMaterialError('当前人物档案、正文状态或卷章来源无法完整核对，请保留原材料并核对当前记录。') from None
    return cards, relationships


def render_character_source(source, project_id, target_volume_idx):
    cards, relationships = character_source_material(source, project_id, target_volume_idx)
    parts = [f'【新弧之前的人物材料：卷 {target_volume_idx} 开始前，正文全局位置小于 {source["before_global_idx"]}】\n'
        '档案不证明正文已经发生；有出处的提取也需要结合原文、人物选择和当前作者要求判断。'
        '同一称谓不证明同一人物，登记别名不能自动合并身份；关系方向不能倒置。'
        '正文状态、正文关系和位置仅保留已核对保存稿来源的记录；档案和作者关系只作为设定材料。'
        '缺少记录不证明人物没有变化或约束。'
        '不要把计划、人物误信或尚未完成的事情改写成确定事实。']
    parts.append('【当前图谱人物档案】')
    for row in source['profiles']:
        origin = '作者编辑的设定' if row['profile_origin'] == 'author_edit' else '登记档案，作者或提取出处未确认'
        parts.append(f'[{row["name"]}｜{origin}]\n' + (row['profile_json'] if row['profile_json'] is not None else '{}'))
    if not source['profiles']: parts.append('当前图谱没有人物档案；不由旧副本补回。')
    parts.append('【逐字段核对来源的正文状态】')
    parts.extend(card.to_prompt() for card in cards)
    if not cards: parts.append('尚无可核对的状态条目，不能据此推定人物一直处于初始状态。')
    parts.append('【按原方向保留的关系】')
    parts.extend(relationships)
    if not relationships: parts.append('尚无可用关系记录；不能推定人物互不认识。')
    parts.append('【按已核对正文来源读取的位置】')
    counts = Counter(row['name'] for row in source['locations'])
    for row in source['locations']:
        parts.append(f'- [CH-{row["global_idx"]}] {row["name"]}：{row["location"]}'
            + ('（同章有多项位置记录，须核对正文内先后，不能按列表顺序决定当前位置）' if counts[row['name']] > 1 else ''))
    if not source['locations']: parts.append('尚无可核对的位置条目。')
    return '\n\n'.join(parts)
