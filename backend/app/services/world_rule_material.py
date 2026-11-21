"""Complete registered settings and their saved outline provenance, not prose facts."""
from copy import deepcopy
import json
import re
from uuid import UUID, NAMESPACE_URL, uuid5

from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest

RULE_FIELDS = {'id', 'category', 'rule_text', 'origin', 'source_outline_id',
               'source_outline_digest', 'source_outline_material_hash'}


def rule_from_properties(properties):
    return {key: properties.get('text' if key == 'rule_text' else key) for key in sorted(RULE_FIELDS)}


def world_rule_version(row):
    return digest({key: row[key] for key in sorted(RULE_FIELDS)})


class WorldRuleSourceError(RequiredContextUnavailable):
    def __init__(self, code, message, *, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_world_rule_source(source, project_id):
    try:
        pid = str(UUID(str(project_id)))
        if (not isinstance(source, dict) or set(source) != {'version', 'project_id', 'book_source', 'rules'}
                or type(source['version']) is not int or source['version'] != 1 or source['project_id'] != pid
                or not isinstance(source['rules'], list)):
            raise ValueError()
        book = source['book_source']
        if book is not None:
            if (not isinstance(book, dict) or set(book) != {'id', 'material_hash', 'legacy_digest'}
                    or str(UUID(book['id'])) != book['id']
                    or book['material_hash'] is not None and not _hash(book['material_hash'])
                    or book['legacy_digest'] is not None and not _hash(book['legacy_digest'])):
                raise ValueError()
        keys, identities = set(), set()
        for row in source['rules']:
            if (not isinstance(row, dict) or set(row) != RULE_FIELDS
                    or not isinstance(row['category'], str) or not isinstance(row['rule_text'], str)
                    or not row['rule_text'].strip()
                    or any(row[key] is not None and not isinstance(row[key], str)
                           for key in RULE_FIELDS - {'category', 'rule_text'})):
                raise ValueError()
            key = (row['category'], row['rule_text'])
            if key in keys or row['id'] is not None and row['id'] in identities:
                raise ValueError()
            keys.add(key)
            if row['id'] is not None:
                identities.add(row['id'])
        json.dumps(source, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WorldRuleSourceError('world_rule_source_invalid', '当前世界规则记录无法完整读取，请保留原规则并核对。') from None
    return source


def rule_source_state(row, book):
    if row['origin'] == 'author':
        return 'author', '作者当前登记的设定；保留的旧提取标记仅作历史参考。'
    oid, original, material_hash = (row['source_outline_id'], row['source_outline_digest'],
                                    row['source_outline_material_hash'])
    if not any((oid, original, material_hash)):
        if row['origin'] == 'outline_extraction':
            return 'needs_review', '这条提取规则缺少书纲来源，请核对后保存为当前设定。'
        return 'registered', '历史登记设定，未提供可核对的提取出处；不证明正文事件已发生。'
    if book is None or oid != book['id']:
        return 'needs_review', '所关联书纲已删除或当前采用了另一份书纲，请核对后保存为当前设定。'
    if material_hash is not None:
        if _hash(material_hash) and material_hash == book['material_hash']:
            return 'current_outline', '提取材料与当前采用的完整书纲一致；这是设定，不是正文事件。'
    elif book['material_hash'] is not None and _hash(original) and original == book['legacy_digest']:
        return 'current_outline', '旧提取来源仍与当前保存来源一致；这是设定，不是正文事件。'
    return 'needs_review', '原提取来源已变化或无法完整核对，请核对规则后保存为当前设定。'


def world_rule_material(source, project_id, *, require_current=True):
    validate_world_rule_source(source, project_id)
    items = []
    for row in source['rules']:
        state, message = rule_source_state(row, source['book_source'])
        if require_current and state == 'needs_review':
            raise WorldRuleSourceError('world_rule_source_requires_review',
                f'规则「{row["category"] or "未分类"}：{row["rule_text"][:60]}」的书纲来源需要核对。请在世界规则中核对并保存当前设定。')
        items.append({**deepcopy(row), 'source_status': state, 'source_message': message})
    return items


def render_world_rules(source, project_id):
    labels = {'author': '作者设定', 'registered': '登记设定，提取出处未知', 'current_outline': '当前书纲提取设定'}
    return [f'[{row["category"] or "未分类"}｜{labels[row["source_status"]]}]\n{row["rule_text"]}'
            for row in world_rule_material(source, project_id)]


def public_rule_identity(project_id, row):
    """Legacy nodes without a UUID get only a deterministic display identifier.

    Canonical edits still match the graph's complete (category, text) key;
    this read identity is never used to invent extraction or author provenance.
    """
    try:
        return str(UUID(row['id']))
    except (ValueError, TypeError, AttributeError):
        return str(uuid5(NAMESPACE_URL, json.dumps([str(project_id), row['category'], row['rule_text']],
                                                  ensure_ascii=False)))
