"""Author source associations: scoped reads and one short SQL transaction."""
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.models.project import Chapter, Volume, Foreshadow, ForeshadowSourceLink as Link, _utcnow
from app.services.entity_materialization_lock import require_materialization_lock
from app.services.entity_sources import digest, resolve_entity_source
from app.services.foreshadow_provenance import foreshadow_source_hash
from app.services.foreshadow_scope import load_foreshadow_positions
from app.services.foreshadow_source_link_contract import (
    SourceLinkConflict, SourceLinkNotFound, linked_source_snapshot, locate_quote, record_snapshot,
)
from app.services.foreshadow_writes import require_project


async def latest_source_links(db, project_id, foreshadow_ids):
    """Read the latest decision, never the most recent still-valid old link."""
    pid = UUID(str(project_id))
    ids = list({UUID(str(value)) for value in foreshadow_ids})
    result = {}
    for start in range(0, len(ids), 500):
        latest = select(Link.foreshadow_id, func.max(Link.revision).label('revision')).where(
            Link.project_id == pid, Link.foreshadow_id.in_(ids[start:start + 500]),
        ).group_by(Link.foreshadow_id).subquery()
        rows = (await db.execute(select(*Link.__table__.columns).join(latest,
            (Link.foreshadow_id == latest.c.foreshadow_id) & (Link.revision == latest.c.revision))
            .where(Link.project_id == pid).execution_options(autoflush=False))).mappings().all()
        result.update({str(row['foreshadow_id']): row for row in rows})
    return result


async def current_record(db, project_id, foreshadow_id, *, lock=False):
    query = select(*Foreshadow.__table__.columns).where(
        Foreshadow.project_id == project_id, Foreshadow.id == foreshadow_id)
    if lock:
        query = query.with_for_update()
    row = (await db.execute(query.execution_options(autoflush=False))).mappings().one_or_none()
    if row is None:
        raise SourceLinkNotFound('foreshadow_unavailable')
    return row


async def current_source(db, project_id, chapter_id, *, lock=False):
    source = await resolve_entity_source(db, project_id, 0, chapter_id,
        lock=lock, allow_review_drafts=True)
    if source is None:
        raise SourceLinkConflict('chapter_unavailable')
    position = (await load_foreshadow_positions(db, project_id)).chapter(chapter_id)
    if position is None or any(str(source.receipt[key]) != str(position[key])
            for key in ('volume_id', 'volume_idx', 'chapter_idx', 'global_idx', 'status')):
        raise SourceLinkConflict('chapter_unavailable')
    return source


async def read_source_chapters(db, project_id, query='', limit=30, offset=0):
    statement = select(Chapter.id, Chapter.title, Chapter.chapter_idx, Chapter.global_idx,
        Volume.id.label('volume_id'), Volume.title.label('volume_title'), Volume.volume_idx).join(
        Volume, Chapter.volume_id == Volume.id).where(Volume.project_id == project_id,
            Chapter.status.in_(('completed', 'draft', 'needs_review')))
    if query:
        matches = [Chapter.title.contains(query, autoescape=True), Volume.title.contains(query, autoescape=True)]
        if query.isdecimal() and 0 < int(query) <= 2147483647:
            matches += [Chapter.chapter_idx == int(query), Chapter.global_idx == int(query)]
        statement = statement.where(or_(*matches))
    rows = list((await db.execute(statement.order_by(Volume.volume_idx, Chapter.chapter_idx, Chapter.id)
        .offset(offset).limit(limit + 1).execution_options(autoflush=False))).mappings().all())
    return {'chapters': rows[:limit], 'has_more': len(rows) > limit}


async def read_source_context(db, project_id, foreshadow_id, chapter_id=None, *, limit=20, offset=0):
    try:
        record = await current_record(db, project_id, foreshadow_id)
    except SourceLinkNotFound:
        record = None
    latest = (await latest_source_links(db, project_id, [foreshadow_id])).get(str(foreshadow_id))
    if record is None and latest is None:
        raise SourceLinkNotFound('foreshadow_unavailable')
    history = list((await db.execute(select(*Link.__table__.columns).where(
        Link.project_id == project_id, Link.foreshadow_id == foreshadow_id)
        .order_by(Link.revision.desc()).offset(offset).limit(limit + 1)
        .execution_options(autoflush=False))).mappings().all())
    preview = None
    if chapter_id is not None:
        source = await current_source(db, project_id, chapter_id)
        snapshot = linked_source_snapshot(source)
        preview = dict(chapter_id=source.chapter_id, title=source.receipt['title'],
            content=source.content, source_snapshot=snapshot, source_version=digest(snapshot))
    description_source = record if record is not None else latest['record_snapshot']
    return dict(foreshadow_id=foreshadow_id, description=description_source['description'], type=description_source['type'],
        record_present=record is not None, record_version=foreshadow_source_hash(record) if record is not None else None,
        latest_link=latest, preview=preview,
        history=history[:limit], has_more=len(history) > limit)


def require_same_command(row, command):
    if (row['project_id'] != command.project_id or row['foreshadow_id'] != command.foreshadow_id
            or row['command_sha256'] != command.sha256 or row['operation'] != command.operation):
        raise SourceLinkConflict('request_id_reused')


async def acknowledgement(db, row):
    latest = (await latest_source_links(db, row['project_id'], [row['foreshadow_id']])).get(str(row['foreshadow_id']))
    return dict(id=row['id'], revision=row['revision'], operation=row['operation'],
        is_latest=latest is not None and latest['id'] == row['id'])


async def apply_source_link(db, command, *, actor=None):
    """The caller commits before acknowledgement; retries never replay edits.

    A source link records the author's explicit chapter identity. It does not
    rewrite legacy chapter numbers, mutate the graph, or declare a payoff.
    """
    await require_materialization_lock(db, command.project_id)
    stored_query = select(*Link.__table__.columns).where(Link.id == command.id).execution_options(autoflush=False)
    stored = (await db.execute(stored_query)).mappings().one_or_none()
    if stored is not None:
        require_same_command(stored, command)
        return await acknowledgement(db, stored)

    await require_project(db, command.project_id)
    # Shared ordering: project/materialization -> chapter/volume -> clue.
    source = None
    if command.operation == 'link':
        source = await current_source(db, command.project_id, command.payload['chapter_id'], lock=True)
    record = await current_record(db, command.project_id, command.foreshadow_id, lock=True)
    if foreshadow_source_hash(record) != command.expected_version:
        raise SourceLinkConflict('record_changed')
    latest = (await latest_source_links(db, command.project_id, [command.foreshadow_id])).get(str(command.foreshadow_id))
    latest_id = str(latest['id']) if latest is not None else None
    if latest_id != command.payload['expected_link_id']:
        raise SourceLinkConflict('link_changed')

    snapshot, start, end = None, None, None
    if source is not None:
        snapshot = linked_source_snapshot(source)
        if digest(snapshot) != command.payload['source_version']:
            raise SourceLinkConflict('chapter_changed')
        start, end = locate_quote(source.content, command.payload['evidence'])
    record_source = record_snapshot(record)
    values = dict(id=command.id, project_id=command.project_id, foreshadow_id=command.foreshadow_id,
        revision=latest['revision'] + 1 if latest is not None else 1,
        previous_id=latest['id'] if latest is not None else None,
        command_sha256=command.sha256, operation=command.operation,
        record_snapshot=record_source, record_sha256=digest(record_source),
        chapter_id=UUID(source.chapter_id) if source is not None else None,
        source_snapshot=snapshot, source_sha256=digest(snapshot) if snapshot is not None else None,
        evidence=command.payload.get('evidence'), evidence_start=start, evidence_end=end,
        note=command.payload['note'], created_by=actor, created_at=_utcnow())
    await db.execute(insert(Link).values(**values).on_conflict_do_nothing(index_elements=['id']))
    stored = (await db.execute(stored_query)).mappings().one()
    require_same_command(stored, command)
    return await acknowledgement(db, stored)
