"""Shared chapter-contract fields and lossless JSON-to-prompt rendering.

Legacy outlines use strings; models and editors may supply structured lists or
objects. Keep their full information when normalizing or rendering a contract.
"""
from __future__ import annotations

import json

CHAPTER_CONTRACT_FIELDS = {
    'start_state': '开场状态', 'end_state': '收束状态', 'time_delta': '时间消耗',
    'location_path': '空间路径', 'entity_transfers': '实体转移', 'information_state': '信息状态',
    'power_resource_map': '权力/资源图', 'mechanism_limits': '机制边界', 'result_strength': '结果强度',
    'action_budget': '动作预算', 'inference_ledger': '推理台账',
    'handoff_to_next': '交接下章', 'transition_bridge': '过桥约束',
    # 本场已定的金额。规划阶段就该定下来:实测写手会在"算一笔钱"的场景里当场
    # 自己发明一个自洽的数,而后一章大纲才写明正确值,读者一眼看出相邻两章对不上。
    'money_totals': '本场已定金额',
}


def render_contract_value(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None or value == {} or value == []:
        return ''
    if isinstance(value, (dict, list, int, float, bool)):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        except (TypeError, ValueError):
            return ''
    return ''


VOLUME_NARRATIVE_FIELDS = (
    'core_conflict', 'emotional_arc', 'transition_to_next', 'volume_start_state',
    'volume_end_state', 'volume_power_resource_map', 'volume_information_map',
    'volume_mechanism_limits', 'volume_result_strength_ladder', 'foreshadow_progression',
)


def validate_volume_meta(meta, volume_idx: int, chapter_range=None) -> None:
    """Check the required planning contract without inventing missing content."""
    if not isinstance(meta, dict) or meta.get('_parse_error') or meta.get('parse_error'):
        raise ValueError('卷元信息必须为完整 JSON 对象')
    if type(meta.get('volume_idx')) is not int or meta['volume_idx'] != volume_idx:
        raise ValueError('volume_idx 必须匹配请求卷号')
    count = meta.get('chapter_count')
    if type(count) is not int or count <= 0:
        raise ValueError('chapter_count 必须为正整数')
    if chapter_range is not None and not chapter_range[0] <= count <= chapter_range[1]:
        raise ValueError('chapter_count 不符合项目章数设置')
    if not isinstance(meta.get('title'), str) or not meta['title'].strip():
        raise ValueError('title 不能为空')

    def has_content(value):
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(has_content(v) for v in value.values())
        if isinstance(value, list):
            return any(has_content(v) for v in value)
        return False

    for key in VOLUME_NARRATIVE_FIELDS:
        if not has_content(meta.get(key)):
            raise ValueError(f'{key} 必须提供具体剧情信息')
    points = meta.get('turning_points')
    if not isinstance(points, list) or not points or not all(has_content(p) for p in points):
        raise ValueError('turning_points 必须包含有效剧情节点')
    for key in ('new_characters', 'departing_characters'):
        if not isinstance(meta.get(key), list):
            raise ValueError(f'{key} 必须为数组，无变化时提供空数组')
    for character in meta['new_characters']:
        if not isinstance(character, dict) or not all(
            isinstance(character.get(key), str) and character[key].strip()
            for key in ('name', 'identity', 'role')
        ):
            raise ValueError('new_characters 条目必须包含 name/identity/role')
    if not all(isinstance(name, str) and name.strip() for name in meta['departing_characters']):
        raise ValueError('departing_characters 必须包含有效角色名称')
    foreshadows = meta.get('foreshadows')
    if not isinstance(foreshadows, dict) or not all(
        isinstance(foreshadows.get(key), list) for key in ('planted', 'resolved')
    ):
        raise ValueError('foreshadows 必须提供 planted/resolved 数组')
    for key in ('planted', 'resolved'):
        if not all(has_content(item) for item in foreshadows[key]):
            raise ValueError(f'foreshadows.{key} 包含空条目')


def chapter_scope_directive(book_outline, volume_outline, chapter_idx, target_word_count=None):
    """Render authoritative scale; only call a chapter final from a complete plan."""
    lines = ['【本章篇幅与收束】',
             '依照全书和本卷既定安排展开本章，不增加章数；计划不是已经发生的事实，前章当前正文决定实际衔接。']
    if type(target_word_count) is int and target_word_count > 0:
        lines.append(f'本章正文目标 {target_word_count} 字，word_count_target 必须为 {target_word_count}；这是正文预算，不是细纲字数。场景数量与展开程度须能在该篇幅内完成。')
    else:
        lines.append('本章未提供独立字数预算，沿用全书/分卷安排，不擅自指定固定 4000 字。')
    plan = book_outline.get('volume_plan') if isinstance(book_outline, dict) else None
    volume = volume_outline if isinstance(volume_outline, dict) else {}
    final = False
    if isinstance(plan, list) and plan:
        indices = [item.get('idx', item.get('volume_idx')) if isinstance(item, dict) else None for item in plan]
        if all(type(idx) is int for idx in indices) and sorted(indices) == list(range(1, len(plan) + 1)):
            count = volume.get('chapter_count')
            final = (type(count) is int and count > 0 and type(chapter_idx) is int
                     and chapter_idx == count and type(volume.get('volume_idx')) is int
                     and volume['volume_idx'] == len(plan))
            last = next(item for item in plan if item.get('idx', item.get('volume_idx')) == len(plan))
            planned_count = last.get('est_chapters', last.get('chapter_count'))
            if planned_count is not None and (type(planned_count) is not int or planned_count != count):
                final = False
    if final:
        lines.append('本章是全书已规划的最后一章。兑现既定结局；next_chapter_hook、handoff_to_next、transition_bridge 写明全书完结及实际收束状态，不虚构下一章、新任务或危机。')
    else:
        lines.append('按全书计划区分普通章节、卷末和全书结尾。有后章时交代必要衔接，不强留悬念；若既定剧情在此完结，交接字段说明收束，不新造续篇。')
    lines.append('细纲写清人物进场、关键对话要点、行动及结果，详略以能够指导正文为准。不要为了字数、转折或事件条数扩充新剧情。')
    return '\n'.join(lines)
