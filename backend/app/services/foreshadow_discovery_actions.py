"""Author decisions and source-checked reading of proposed prose clues."""
from uuid import UUID
import json

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.project import (Project, Foreshadow, ForeshadowWriteReceipt,
    ForeshadowDiscovery as Candidate, ForeshadowDiscoveryRun as Run, ForeshadowDiscoveryRunItem as RunItem, _utcnow)
from app.services.entity_sources import resolve_entity_source
from app.services.foreshadow_discovery import parse_candidates
from app.services.foreshadow_discovery_reader import current_sources, snapshot_state, require_discovery_run


class DiscoveryNotFound(Exception):
    pass


class DiscoveryAlreadyHandled(Exception):
    pass


class DiscoverySourceChanged(Exception):
    pass


class DiscoveryWriteConflict(Exception):
    pass


class DiscoveryProjectUnavailable(Exception):
    pass


def candidate_query(project_id):
    return select(*Candidate.__table__.columns, Run.project_id, Run.chapter_id,
        Run.source_sha256, Run.source_snapshot).join(Run, Candidate.run_id == Run.id).where(
        Run.project_id == project_id).execution_options(autoflush=False)


def candidate_source_state(row, source):
    state = snapshot_state(row, source)
    if state != 'current':
        return state
    try:
        candidates = parse_candidates(json.dumps([{key: row[key] for key in ('description', 'type', 'evidence', 'reason')}]), source.content)
        candidate = candidates[0]
        if (type(row['evidence_start']) is not int or type(row['evidence_end']) is not int
                or (candidate.evidence_start, candidate.evidence_end) != (row['evidence_start'], row['evidence_end'])):
            return 'unknown'
        return 'current'
    except (KeyError, ValueError, TypeError, AttributeError, RuntimeError):
        return 'unknown'


async def list_discoveries(db, project_id, status='pending', limit=20, offset=0, *, run_id=None):
    query = candidate_query(project_id)
    if run_id is not None:
        await require_discovery_run(db, project_id, run_id)
        query = query.add_columns(RunItem.reason.label('scan_reason')).join(RunItem, RunItem.candidate_id == Candidate.id).where(
            RunItem.run_id == run_id)
    if status is not None:
        query = query.where(Candidate.status == status)
    order = (RunItem.position,) if run_id is not None else (Candidate.created_at.desc(), Candidate.id.desc())
    rows = list((await db.execute(query.order_by(*order)
        .offset(offset).limit(limit + 1))).mappings().all())
    has_more, rows = len(rows) > limit, rows[:limit]
    if not rows:
        return {'discoveries': [], 'has_more': has_more}
    sources = await current_sources(db, project_id, {row['chapter_id'] for row in rows})
    accepted_ids = {row['foreshadow_id'] for row in rows if row['status'] == 'accepted' and row['foreshadow_id']}
    present_ids = set((await db.execute(select(Foreshadow.id).where(Foreshadow.project_id == project_id,
        Foreshadow.id.in_(accepted_ids)).execution_options(autoflush=False))).scalars().all()) if accepted_ids else set()
    result = []
    for row in rows:
        state = candidate_source_state(row, sources.get(row['chapter_id']))
        result.append(dict(row, source_state=state, reused_in_scan=run_id is not None and row['run_id'] != run_id,
            source_snapshot=row['source_snapshot'] if state != 'unknown' else {},
            record_present=(row['foreshadow_id'] in present_ids) if row['status'] == 'accepted' else None))
    return {'discoveries': result, 'has_more': has_more}


async def accept_discovery(db, project_id: UUID, candidate_id: UUID, *, actor=None) -> UUID:
    """Create a PG-owned author record and its decision in one transaction.

    The candidate ID is the new record's identity. Acknowledged retries never
    recreate a deleted record or reset later edits. No cross-store write or
    nested commit may release the chapter source lock before this decision.
    """
    query = candidate_query(project_id).where(Candidate.id == candidate_id)
    initial = (await db.execute(query)).mappings().one_or_none()
    if initial is None:
        raise DiscoveryNotFound()
    if initial['status'] == 'accepted':
        if initial['foreshadow_id'] is None:
            raise DiscoveryWriteConflict()
        return initial['foreshadow_id']
    if initial['status'] != 'pending':
        raise DiscoveryAlreadyHandled()
    from app.services.entity_materialization_lock import require_materialization_lock
    await require_materialization_lock(db, project_id)
    # Keep project purge/soft delete out of this new author write. Historical
    # acknowledgements above remain readable without replaying a mutation.
    project = (await db.execute(select(Project.id).where(Project.id == project_id,
        Project.deleted_at.is_(None)).with_for_update(read=True)
        .execution_options(autoflush=False))).scalar_one_or_none()
    if project is None:
        raise DiscoveryProjectUnavailable()
    # Same source-before-candidate lock order as worker persistence.
    source = await resolve_entity_source(db, project_id, 0, initial['chapter_id'], lock=True, allow_review_drafts=True)
    row = (await db.execute(query.with_for_update(of=Candidate))).mappings().one_or_none()
    if row is None:
        raise DiscoveryNotFound()
    if row['status'] == 'accepted':
        if row['foreshadow_id'] is None:
            raise DiscoveryWriteConflict()
        return row['foreshadow_id']
    if row['status'] != 'pending':
        raise DiscoveryAlreadyHandled()
    if row['chapter_id'] != initial['chapter_id'] or candidate_source_state(row, source) != 'current':
        raise DiscoverySourceChanged()
    # Direct creates use their request ID as the clue ID. A graph create may
    # already own that identity while its SQL projection is still missing.
    reserved = (await db.execute(select(ForeshadowWriteReceipt.id).where(
        ForeshadowWriteReceipt.id == candidate_id).execution_options(autoflush=False))).scalar_one_or_none()
    if reserved is not None:
        raise DiscoveryWriteConflict()
    inserted = (await db.execute(insert(Foreshadow).values(
        id=candidate_id, project_id=project_id, description=row['description'], type=row['type'],
        planted_chapter=source.global_idx, resolve_conditions_json=[], resolution_blueprint_json={},
        status='planted', resolved_chapter=None, narrative_proximity=0.0, source='lifecycle', created_at=_utcnow(),
    ).on_conflict_do_nothing(index_elements=['id']).returning(Foreshadow.id))).scalar_one_or_none()
    if inserted is None:
        # A collision is not proof that this candidate was already accepted.
        raise DiscoveryWriteConflict()
    await db.execute(update(Candidate).where(Candidate.id == candidate_id).values(
        status='accepted', foreshadow_id=candidate_id, handled_at=_utcnow(), handled_by=actor,
    ).execution_options(synchronize_session=False, autoflush=False))
    return candidate_id


async def dismiss_discovery(db, project_id: UUID, candidate_id: UUID, *, actor=None):
    """Ignoring stale or unwanted evidence requires no current source."""
    scope = select(Run.id).where(Run.project_id == project_id)
    changed = (await db.execute(update(Candidate).where(Candidate.run_id.in_(scope),
        Candidate.id == candidate_id, Candidate.status == 'pending').values(
        status='dismissed', handled_at=_utcnow(), handled_by=actor).returning(Candidate.id)
        .execution_options(synchronize_session=False, autoflush=False))).scalar_one_or_none()
    if changed is not None:
        return
    status = (await db.execute(select(Candidate.status).where(Candidate.id == candidate_id,
        Candidate.run_id.in_(scope)).execution_options(autoflush=False))).scalar_one_or_none()
    if status is None:
        raise DiscoveryNotFound()
    if status != 'dismissed':
        raise DiscoveryAlreadyHandled()
