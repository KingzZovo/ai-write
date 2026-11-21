"""Resolve registered prose clues from retained author decisions and exact text."""
from collections import defaultdict
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.models.project import Chapter, Volume, ForeshadowDiscovery as Candidate
from app.services.foreshadow_discovery_actions import candidate_query, candidate_source_state
from app.services.foreshadow_scope import load_foreshadow_positions
from app.services.foreshadow_source_links import latest_source_links
from app.services.foreshadow_source_link_contract import author_source_state


async def inspect_foreshadow_sources(db, project_id, records, *, positions=None, boundary=None):
    """Unknown historical numbers cannot authorize chronology comparisons.

    A saved author decision is evidence only for its original description,
    type, chapter and full prose. Later descriptive edits need fresh evidence.
    Source bodies are fetched once per chapter in bounded batches, and only
    for potentially prior sources when generating a chapter.
    """
    pid = UUID(str(project_id))
    records = {str(row['id']): row for row in records}
    result = {fid: {'source_state': 'unknown', 'source_kind': 'record',
                    'source_label': '来源未核实', 'source_evidence': None} for fid in records}
    if not records:
        return result
    positions = positions or await load_foreshadow_positions(db, pid)
    author_links = await latest_source_links(db, pid, records)
    candidates = defaultdict(list)
    ids = [UUID(fid) for fid in records if fid not in author_links]
    for start in range(0, len(ids), 500):
        rows = (await db.execute(candidate_query(pid).where(Candidate.status == 'accepted',
            Candidate.foreshadow_id.in_(ids[start:start + 500])))).mappings().all()
        for row in rows:
            candidates[str(row['foreshadow_id'])].append(row)
    eligible = {}
    for fid, row in author_links.items():
        result[fid].update(source_origin='author', source_link_id=row['id'])
        if row['operation'] == 'unlink':
            result[fid]['source_label'] = '作者已解除正文关联'
            continue
        result[fid].update(source_kind='prose', source_evidence=row['evidence'])
        initial_state = author_source_state(row, records[fid], None)
        if initial_state != 'unavailable':
            result[fid]['source_state'] = initial_state
            continue
        position = positions.chapter(row['chapter_id'])
        if position is None:
            result[fid].update(source_state='unavailable', source_label='正文来源或章节位置不可用')
            continue
        result[fid].update(source_chapter_id=row['chapter_id'], source_global_idx=position['global_idx'],
            source_label=f"第{position['volume_idx']}卷第{position['chapter_idx']}章（全书位置{position['global_idx']}）")
        if boundary is not None and not boundary.includes(position):
            result[fid]['source_state'] = 'outside_context'
            continue
        eligible[fid] = row
    for fid, matches in candidates.items():
        if len(matches) != 1:
            continue
        row, record = matches[0], records[fid]
        result[fid].update(source_kind='prose', source_origin='candidate', source_evidence=row['evidence'])
        position = positions.chapter(row['chapter_id'])
        if position is None:
            result[fid].update(source_state='unavailable', source_label='正文来源或章节位置不可用')
            continue
        result[fid]['source_label'] = (f"第{position['volume_idx']}卷第{position['chapter_idx']}章"
                                       f"（全书位置{position['global_idx']}）")
        result[fid].update(source_chapter_id=row['chapter_id'], source_global_idx=position['global_idx'])
        if (record['source'] != 'lifecycle' or record['description'] != row['description']
                or record['type'] != row['type'] or record['planted_chapter'] != position['global_idx']):
            result[fid]['source_state'] = 'changed'
            continue
        if boundary is not None and not boundary.includes(position):
            result[fid]['source_state'] = 'outside_context'
            continue
        eligible[fid] = row
    bodies = {}
    chapter_ids = list({row['chapter_id'] for row in eligible.values()})
    for start in range(0, len(chapter_ids), 500):
        rows = (await db.execute(select(Chapter.id, Chapter.content_text, Chapter.volume_id, Chapter.chapter_idx,
            Chapter.global_idx, Chapter.status, Volume.volume_idx).join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == pid, Chapter.id.in_(chapter_ids[start:start + 500]))
            .execution_options(autoflush=False))).mappings().all()
        bodies.update({str(row['id']): row for row in rows})
    for fid, row in eligible.items():
        position = positions.chapter(row['chapter_id'])
        fresh = bodies.get(str(row['chapter_id']))
        if fresh is not None and any(fresh[key] != position[key] for key in ('volume_id', 'volume_idx', 'chapter_idx', 'global_idx', 'status')):
            result[fid]['source_state'] = 'changed'
            continue
        body = fresh['content_text'] if fresh is not None else None
        source = (SimpleNamespace(project_id=pid, chapter_id=row['chapter_id'], global_idx=position['global_idx'],
            content=body, receipt={'volume_id': position['volume_id'], 'chapter_idx': position['chapter_idx'],
                                  'volume_idx': position['volume_idx']})
            if body is not None and position['status'] in {'completed', 'draft', 'needs_review'} else None)
        result[fid]['source_state'] = (author_source_state(row, records[fid], source)
            if fid in author_links else candidate_source_state(row, source))
    return result
