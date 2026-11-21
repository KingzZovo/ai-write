"""Read plans from their current outlines; never copy them into prose facts."""
import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import select

from app.models.project import Chapter, Outline, Volume
from app.services.foreshadow_scope import load_foreshadow_positions, positive_index
from app.services.outline_authority import TEXT_REVISION
from app.services.outline_order import REF, UNAVAILABLE

PLAN_NAMESPACE = UUID('343c35f0-78c0-461f-858a-d00ea5607ce8')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def plan_entries(raw):
    """Validate the whole source array without shortening descriptions/hints.

    An explicitly supplied planted_chapter is always volume-local. A volume
    plan without it stays unscheduled; a chapter plan belongs to its actual
    chapter. No number is guessed from its magnitude.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('invalid_foreshadow_plan_array')
    result, seen = [], set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {'description': entry}
        if not isinstance(entry, dict):
            raise ValueError('invalid_foreshadow_plan_entry')
        description = entry.get('description') or entry.get('desc') or entry.get('text')
        typ = entry.get('type') or entry.get('category') or '伏笔'
        if (not isinstance(description, str) or not description.strip()
                or not isinstance(typ, str) or not typ.strip() or len(typ.strip()) > 20):
            raise ValueError('invalid_foreshadow_plan_entry')
        local_idx = entry.get('planted_chapter')
        if local_idx is not None and not positive_index(local_idx):
            raise ValueError('invalid_foreshadow_plan_position')
        hint = next((entry[key] for key in ('resolve_conditions', 'hint', 'resolve_hint')
                     if key in entry and entry[key] is not None), None)
        if hint is not None and not isinstance(hint, (str, list, dict)):
            raise ValueError('invalid_foreshadow_plan_hint')
        normalized = {'description': description.strip(), 'type': typ.strip(),
                      'chapter_idx': local_idx, 'hint': hint}
        if REF in entry:
            if not isinstance(entry[REF], str) or not entry[REF] or entry.get(UNAVAILABLE):
                raise ValueError('foreshadow_plan_identity_unavailable')
            normalized['chapter_id'] = entry[REF]
        fingerprint = _digest(normalized)
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(normalized)
    return result


def _render_hint(hint):
    if hint is None:
        return ''
    return hint if isinstance(hint, str) else json.dumps(hint, ensure_ascii=False, allow_nan=False)


async def read_foreshadow_plans(db, project_id, *, positions=None, boundary=None):
    """Derived view shared by generation and the author panel.

    Read small outline fields, not all chapter bodies/raw model responses.
    An empty newer selected outline suppresses an older populated version.
    Invalid source arrays are reported as unavailable, never partially read.
    """
    pid = UUID(str(project_id))
    positions = positions or await load_foreshadow_positions(db, pid)
    volume_rows = (await db.execute(select(
        Outline.id, Outline.is_confirmed, Outline.content_json['volume_id'].as_string().label('volume_id'),
        Outline.content_json['volume_idx'].as_string().label('volume_idx'),
        Outline.content_json['foreshadows'].label('foreshadows'),
        Outline.content_json[TEXT_REVISION].as_string().label('text_revision'),
    ).where(Outline.project_id == pid, Outline.level == 'volume')
      .order_by(Outline.is_confirmed.desc(), Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
      .execution_options(autoflush=False))).mappings().all()
    selected = {}
    for row in volume_rows:
        volume_id = row['volume_id']
        if volume_id is not None:
            # A stale/foreign stable ID cannot fall back to a coincident number.
            volume = positions.volume(volume_id)
        else:
            matches = [v for v in positions.volumes.values() if str(v['volume_idx']) == str(row['volume_idx'])]
            volume = positions.volume(matches[0]['id']) if len(matches) == 1 else None
        if volume is not None:
            selected.setdefault(str(volume['id']), (row, volume))
    chapter_rows = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Volume.volume_idx, Chapter.outline_json['foreshadows_planted'].label('entries'),
        Chapter.outline_json[TEXT_REVISION].as_string().label('text_revision'))
        .join(Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == pid)
        .execution_options(autoflush=False))).mappings().all()
    plans, unavailable_sources = [], 0

    def append_source(raw, *, kind, source_id, volume, chapter=None, confirmed=None):
        nonlocal unavailable_sources
        if boundary is not None:
            if chapter is not None:
                if not boundary.includes(chapter, planned=True):
                    return
            elif (volume['volume_idx'] > boundary.volume_idx or
                  (boundary.chapter_idx is None and volume['volume_idx'] == boundary.volume_idx)):
                return
        try:
            entries = plan_entries(raw)
            pending = []
            for entry in entries:
                position = chapter
                if 'chapter_id' in entry:
                    position = positions.chapter(entry['chapter_id'])
                    if (position is None or str(position['volume_id']) != str(volume['id'])
                            or (chapter is not None and str(position['id']) != str(chapter['id']))):
                        raise ValueError('foreshadow_plan_identity_unavailable')
                elif chapter is not None and entry['chapter_idx'] not in (None, chapter['chapter_idx']):
                    raise ValueError('chapter_plan_position_mismatch')
                elif chapter is None and entry['chapter_idx'] is not None:
                    position = positions.at_local(volume['id'], entry['chapter_idx'])
                    if position is None:
                        raise ValueError('volume_plan_position_unavailable')
                if position is not None and boundary is not None and not boundary.includes(position, planned=True):
                    continue
                label = f"第{volume['volume_idx']}卷"
                label += (f"第{position['chapter_idx']}章（全书位置{position['global_idx']}）" if position
                          else '卷级计划（未指定章节）')
                payload = {'source_kind': kind, 'source_id': str(source_id), 'volume_id': str(volume['id']),
                    'volume_idx': volume['volume_idx'], 'chapter_id': str(position['id']) if position else None,
                    'chapter_idx': position['chapter_idx'] if position else None,
                    'planted_chapter': position['global_idx'] if position else None,
                    'description': entry['description'], 'type': entry['type'], 'hint': _render_hint(entry['hint']),
                    'status': 'planned', 'source_label': label, 'is_confirmed': confirmed}
                pending.append({'id': str(uuid5(PLAN_NAMESPACE, _digest(payload))), **payload})
            plans.extend(pending)
        except (TypeError, ValueError, OverflowError):
            unavailable_sources += 1

    for row, volume in selected.values():
        if row['text_revision']:
            continue
        if boundary is not None and (volume['volume_idx'] > boundary.volume_idx or
                (boundary.chapter_idx is None and volume['volume_idx'] == boundary.volume_idx)):
            continue
        block = row['foreshadows']
        if block is None:
            continue
        if not isinstance(block, dict):
            unavailable_sources += 1
            continue
        append_source(block.get('planted'), kind='volume_outline', source_id=row['id'],
                      volume=volume, confirmed=row['is_confirmed'] == 1)
    for row in chapter_rows:
        if row['entries'] is None or row['text_revision']:
            continue
        if boundary is not None and (row['volume_idx'] > boundary.volume_idx or
                (row['volume_idx'] == boundary.volume_idx and
                 (boundary.chapter_idx is None or row['chapter_idx'] > boundary.chapter_idx))):
            continue
        chapter = positions.chapter(row['id'])
        if chapter is None or any(row[key] != chapter[key] for key in ('volume_id', 'volume_idx', 'chapter_idx', 'global_idx')):
            unavailable_sources += 1
            continue
        append_source(row['entries'], kind='chapter_outline', source_id=row['id'],
                      volume=positions.volume(chapter['volume_id']), chapter=chapter)
    plans.sort(key=lambda p: (p['volume_idx'], p['chapter_idx'] or 0, p['source_kind'], p['id']))
    return {'plans': plans, 'unavailable_sources': unavailable_sources}
