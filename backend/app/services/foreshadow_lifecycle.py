"""Source-checked foreshadow references, with plans kept in their outlines.

Outlines are the source of truth for planning. Reading them directly avoids
stale mutable copies, invented planting dates and a second commit after save.
Only author-confirmed, current prose sources authorize historical placement.
"""
from uuid import UUID

from sqlalchemy import select

from app.models.project import Foreshadow
from app.services.foreshadow_plans import read_foreshadow_plans, _render_hint
from app.services.foreshadow_scope import load_foreshadow_positions, positive_index
from app.services.foreshadow_sources import inspect_foreshadow_sources


async def chapter_global_idx(db, project_id, volume_idx, chapter_idx_local):
    """Read the uniquely identified chapter's persisted position, never count.

    Retained for callers that identify the target by volume-local numbers.
    Historical Foreshadow.planted_chapter values are not converted here.
    """
    positions = await load_foreshadow_positions(db, project_id)
    if not positive_index(volume_idx) or not positive_index(chapter_idx_local):
        raise ValueError('foreshadow_chapter_position_unavailable')
    volumes = [v for v in positions.volumes.values() if v['volume_idx'] == volume_idx]
    position = positions.at_local(volumes[0]['id'], chapter_idx_local) if len(volumes) == 1 else None
    if position is None:
        raise ValueError('foreshadow_chapter_position_unavailable')
    return position['global_idx']


async def load_active_foreshadows_for_context(
    db, project_id, upcoming_global_chapter_idx=None, limit=25, *, target_volume_idx=None,
):
    """Read prior prose and applicable outline plans under an explicit bound.

    Unknown/stale historical records stay in the author panel, without being
    supplied as known history. limit=None retains all valid references for
    callers with an explicit whole-context budget; planning uses a small view.
    """
    pid = UUID(str(project_id))
    positions = await load_foreshadow_positions(db, pid)
    boundary = positions.boundary(upcoming_global_chapter_idx, volume_idx=target_volume_idx)
    records = list((await db.execute(select(*Foreshadow.__table__.columns).where(
        Foreshadow.project_id == pid, Foreshadow.status.in_(('planted', 'ripening', 'ready')))
        .order_by(Foreshadow.planted_chapter.desc(), Foreshadow.id)
        .execution_options(autoflush=False))).mappings().all())
    sources = await inspect_foreshadow_sources(db, pid, records, positions=positions, boundary=boundary)
    items = []
    for row in records:
        source = sources[str(row['id'])]
        if source['source_state'] != 'current':
            continue
        blueprint = row['resolution_blueprint_json']
        items.append({**dict(row), **source, 'hint': _render_hint(row['resolve_conditions_json']),
                      'planted_chapter': source['source_global_idx'],
                      'blueprint': blueprint if isinstance(blueprint, dict) else {}})
    items.sort(key=lambda item: (-item['planted_chapter'], str(item['id'])))
    plans = await read_foreshadow_plans(db, pid, positions=positions, boundary=boundary)
    if plans['unavailable_sources']:
        raise ValueError('foreshadow_plan_sources_unavailable')
    # Most recent applicable plans supplement registered prose, without
    # assigning arbitrary payoff deadlines or pretending they were planted.
    items.extend(reversed(plans['plans']))
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError('invalid_foreshadow_context_limit')
        items = items[:limit]
    return items


def format_active_foreshadows_for_prompt(items):
    if not items:
        return ''
    lines = [
        '【有来源的伏笔记录与大纲计划】',
        '计划只是写作安排，不证明前文已埋设或已经兑现，也不要求补写到本章。',
        '已登记线索附保存稿原句；结合当前正文、人物认识和行动选择推进、暂时悬置或回应。',
        '不必让每条线索在本章出现；不要凭章数、状态或分数安排收线。',
    ]
    for item in items:
        planned = item.get('status') == 'planned'
        label = item.get('source_label') or '位置未指定'
        lines.append(f"- [{'计划·尚待正文落实' if planned else '作者已登记·保存稿证据'}] {label}：{item['description']}")
        if item.get('source_evidence'):
            lines.append('  原句：' + item['source_evidence'])
        if item.get('hint'):
            lines.append('  待核对条件/计划：' + item['hint'])
        if item.get('blueprint'):
            lines.append('  可选回应计划：' + _render_hint(item['blueprint']))
    return '\n'.join(lines)
