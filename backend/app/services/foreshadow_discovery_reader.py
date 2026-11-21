"""Read full-prose scan history without treating a missing scan as an empty one."""
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models.project import Chapter, Volume, ForeshadowDiscoveryRun as Run
from app.services.entity_sources import digest
from app.services.foreshadow_discovery import source_snapshot, read_scan_members, complete_scan_receipt


class DiscoveryRunNotFound(Exception):
    pass


async def current_sources(db, project_id, chapter_ids):
    """Load each full source once per page and reject ambiguous saved positions."""
    if not chapter_ids:
        return {}
    chapters = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Chapter.content_text, Chapter.status).join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id, Chapter.id.in_(chapter_ids))
        .execution_options(autoflush=False))).mappings().all()
    indices = {c['global_idx'] for c in chapters if c['global_idx']}
    counts = dict((await db.execute(select(Chapter.global_idx, func.count(Chapter.id))
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id, Chapter.global_idx.in_(indices)).group_by(Chapter.global_idx)
        .execution_options(autoflush=False))).all()) if indices else {}
    return {c['id']: SimpleNamespace(project_id=project_id, chapter_id=c['id'], global_idx=c['global_idx'],
        content=c['content_text'], receipt={'volume_id': c['volume_id'], 'chapter_idx': c['chapter_idx']})
        for c in chapters if c['status'] in {'completed', 'draft', 'needs_review'} and counts.get(c['global_idx']) == 1}


def snapshot_state(row, source):
    snapshot = row['source_snapshot']
    try:
        if (not isinstance(snapshot, dict) or digest(snapshot) != row['source_sha256']
                or snapshot.get('project_id') != str(row['project_id'])
                or snapshot.get('chapter_id') != str(row['chapter_id'])):
            return 'unknown'
        if source is None:
            return 'unavailable'
        return 'current' if snapshot == source_snapshot(source) else 'changed'
    except (KeyError, ValueError, TypeError, AttributeError, RuntimeError):
        return 'unknown'


async def require_discovery_run(db, project_id, run_id):
    present = (await db.execute(select(Run.id).where(Run.project_id == project_id, Run.id == run_id)
        .execution_options(autoflush=False))).scalar_one_or_none()
    if present is None:
        raise DiscoveryRunNotFound()


async def list_discovery_runs(db, project_id, *, chapter_id=None, limit=20, offset=0):
    live_chapters = select(Chapter.id, Chapter.title).join(Volume, Chapter.volume_id == Volume.id).where(
        Volume.project_id == project_id).subquery()
    query = select(*Run.__table__.columns, live_chapters.c.title.label('chapter_title')).outerjoin(
        live_chapters, Run.chapter_id == live_chapters.c.id).where(Run.project_id == project_id)
    if chapter_id is not None:
        query = query.where(Run.chapter_id == chapter_id)
    rows = (await db.execute(query.order_by(Run.created_at.desc(), Run.id.desc()).offset(offset).limit(limit + 1)
        .execution_options(autoflush=False))).mappings().all()
    has_more, rows = len(rows) > limit, rows[:limit]
    sources = await current_sources(db, project_id, {row['chapter_id'] for row in rows})
    members = await read_scan_members(db, [row['id'] for row in rows])
    results = []
    for row in rows:
        state = snapshot_state(row, sources.get(row['chapter_id']))
        complete = complete_scan_receipt(row, members.get(row['id'], []))
        results.append(dict(row, source_state=state, result_state='complete' if complete else 'unknown',
            candidate_count=row['candidate_count'] if complete else None,
            source_snapshot=row['source_snapshot'] if state != 'unknown' else {}))
    return dict(runs=results, has_more=has_more)
