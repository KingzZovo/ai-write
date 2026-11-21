"""Pure, complete book-level planning material, distinct from prose facts."""
from copy import deepcopy
import json
from uuid import UUID

from app.services.outline_authority import TEXT_REVISION, outline_for_generation
from app.services.outline_order import REF, UNAVAILABLE
from app.services.world_rule_material import WorldRuleSourceError, world_rule_material


class PlanningBookMaterialError(ValueError):
    pass


def _identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('invalid identity')


def _text(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not optional and not value.strip():
        raise ValueError('invalid text')


def book_source_material(source, project_id):
    """Keep stored originals untouched; apply the existing author-text authority."""
    try:
        if (not isinstance(source, dict) or set(source) != {'version', 'project', 'outline', 'world_rules'}
                or type(source['version']) is not int or source['version'] not in (1, 2)):
            raise ValueError('invalid source')
        project = source['project']
        if (not isinstance(project, dict) or set(project) != {'id', 'title', 'genre', 'premise', 'target_word_count'}
                or project['id'] != str(project_id)):
            raise ValueError('invalid project')
        _identity(project['id']); _text(project['title'])
        _text(project['genre'], optional=True); _text(project['premise'], optional=True)
        if type(project['target_word_count']) is not int or project['target_word_count'] <= 0:
            raise ValueError('invalid book scale')
        outline, projected = source['outline'], None
        if outline is not None:
            if (not isinstance(outline, dict) or set(outline) != {'id', 'project_id', 'version', 'is_confirmed', 'content_json'}
                    or outline['project_id'] != str(project_id)
                    or type(outline['version']) is not int or outline['version'] <= 0
                    or type(outline['is_confirmed']) is not int or outline['is_confirmed'] not in (0, 1)):
                raise ValueError('invalid outline owner')
            _identity(outline['id'])
            original = outline['content_json']
            if (not isinstance(original, dict) or original.get('parse_error') or original.get('_parse_error')
                    or 'available' in original and original['available'] is not True
                    or original.get(UNAVAILABLE) or REF in original and not original[REF]
                    or 'raw_text' in original and not isinstance(original['raw_text'], str)):
                raise ValueError('invalid current outline')
            if original.get(TEXT_REVISION):
                _text(original.get('raw_text'))
            projected = outline_for_generation(original)
            if not isinstance(projected, dict) or not projected:
                raise ValueError('unavailable current outline')
        if source['version'] == 2:
            rules = [{key: row[key] for key in ('category', 'rule_text', 'source_status')}
                     for row in world_rule_material(source['world_rules'], project_id)]
            rule_rows = []
        else:
            if not isinstance(source['world_rules'], list):
                raise ValueError('invalid world rules')
            rule_rows = source['world_rules']
            rules = []
        identities = set()
        for rule in rule_rows:
            if not isinstance(rule, dict) or set(rule) != {'id', 'category', 'rule_text'}:
                raise ValueError('invalid rule')
            _identity(rule['id']); _text(rule['rule_text'])
            if not isinstance(rule['category'], str):
                raise ValueError('invalid category')
            if rule['id'] in identities:
                raise ValueError('duplicate rule')
            identities.add(rule['id'])
            rules.append({key: rule[key] for key in ('category', 'rule_text')})
        json.dumps(source, ensure_ascii=False, allow_nan=False)
    except WorldRuleSourceError as exc:
        raise PlanningBookMaterialError(str(exc)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningBookMaterialError('本书立意、当前书纲或世界规则无法完整核对，请保留原材料并修正当前来源。') from None
    return {'project': {key: deepcopy(value) for key, value in project.items() if key != 'id'},
            'outline': deepcopy(projected), 'outline_confirmed': outline['is_confirmed'] if outline else None,
            'world_rules': rules}


def render_book_source(source, project_id):
    material = book_source_material(source, project_id)
    project = material['project']
    parts = ['【本书创作材料：设定与计划不能证明事件已经发生】\n'
        '这些是作品原始立意、当前书纲和登记规则，不是正文事实提取；'
        '规则可能来自作者编辑或已有提取，不能据此补造过去的事件。'
        '本次作者明确修改设定时，保留修改的范围，不用旧计划覆盖当前要求。'
        '全书未来安排只用于约束当前弧的范围，不能为本弧收束提前兑现后续结局。',
        f'作品：{project["title"]}\n题材：{project["genre"] or "未填写"}\n'
        f'原目标规模：{project["target_word_count"]} 字\n原始立意：\n{project["premise"] or "未填写"}']
    outline = material['outline']
    if outline is None:
        parts.append('【全书大纲】本书尚无已保存书纲；不把缺少计划解释为没有长线。')
    else:
        label = '当前采用的书纲' if material['outline_confirmed'] == 1 else '尚未确认的书纲候选'
        parts.append(f'【{label}：仍是计划，不是已发生正文】')
        raw = outline.get('raw_text')
        if isinstance(raw, str):
            parts.append(raw)
            rest = {key: value for key, value in outline.items() if key != 'raw_text'}
        else:
            rest = outline
        if rest:
            parts.append(json.dumps(rest, ensure_ascii=False, allow_nan=False, indent=2))
    parts.append('【已登记世界规则】')
    if material['world_rules']:
        labels = {'author': '作者设定', 'registered': '登记设定，提取出处未知', 'current_outline': '当前书纲提取设定'}
        for rule in material['world_rules']:
            origin = f'｜{labels[rule["source_status"]]}' if 'source_status' in rule else ''
            parts.append(f'[{rule["category"] if rule["category"].strip() else "未分类"}{origin}]\n{rule["rule_text"]}')
    else:
        parts.append('尚无已登记条目；不能推定人物没有其他限制。')
    return '\n\n'.join(parts)
