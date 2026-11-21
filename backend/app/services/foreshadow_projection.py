"""Project one current graph record under the shared materialization mutex."""
import math
from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.models.project import Foreshadow
from app.services.foreshadow_graph_confirmation import projected_foreshadow


class ForeshadowProjectionConflict(RuntimeError):
    pass


async def project_foreshadow_rows(db, project_id, rows):
    """Batch projection preserves ownership and reports every ID collision.

    Caller owns the materialization mutex and transaction. Conflict predicates
    also protect against another project's concurrent claim of a missing ID.
    JSON/unchanged-value comparison remains in the existing bulk helper.
    """
    from app.services.entity_bulk import upsert_projection_rows
    project_id = UUID(str(project_id))
    normalized, ids = [], set()
    for row in rows:
        if str(row.get('project_id')) != str(project_id) or row.get('source') != 'neo4j':
            raise ForeshadowProjectionConflict('foreshadow_projection_scope_changed')
        fid = UUID(str(row['id']))
        if fid in ids:
            raise ForeshadowProjectionConflict('foreshadow_duplicate_graph_identity')
        ids.add(fid)
        normalized.append(dict(row, id=fid, project_id=project_id))
    changed = 0
    for start in range(0, len(normalized), 500):
        batch = normalized[start:start + 500]
        changed += await upsert_projection_rows(db, Foreshadow, batch,
            update_columns=('type', 'description', 'planted_chapter', 'resolve_conditions_json',
                            'resolution_blueprint_json', 'narrative_proximity', 'status', 'resolved_chapter'),
            update_condition=and_(Foreshadow.project_id == project_id, Foreshadow.source == 'neo4j'))
        # ON CONFLICT with a false predicate still locks the conflicting row.
        # A no-op because of an unchanged value is safe; a foreign owner is not.
        current = (await db.execute(select(Foreshadow.id, Foreshadow.project_id, Foreshadow.source).where(
            Foreshadow.id.in_([row['id'] for row in batch])).execution_options(autoflush=False))).all()
        if len(current) != len(batch) or any(row.project_id != project_id or row.source != 'neo4j' for row in current):
            raise ForeshadowProjectionConflict('foreshadow_owner_changed')
    return changed


async def project_current_foreshadow(db, driver, project_id, foreshadow_id):
    """Caller holds the materialization mutex from graph read through SQL commit.

    Return the projected row, never a value copied from an older write receipt.
    A conflicting owner/ID must not be silently counted as a successful sync.
    """
    async with driver.session() as session:
        row = await (await session.run(
            'MATCH (f:Foreshadow {project_id:$pid,id:$fid}) RETURN properties(f) AS node',
            pid=str(project_id), fid=str(foreshadow_id))).single()
    if row is None:
        existing = (await db.execute(select(Foreshadow.source).where(
            Foreshadow.id == foreshadow_id, Foreshadow.project_id == project_id)
            .execution_options(autoflush=False))).one_or_none()
        if existing is not None and existing[0] != 'neo4j':
            raise ForeshadowProjectionConflict('foreshadow_owner_changed')
        await db.execute(delete(Foreshadow).where(Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id, Foreshadow.source == 'neo4j'))
        return None
    source = projected_foreshadow(row['node'])
    proximity = row['node'].get('narrative_proximity', 0)
    source['narrative_proximity'] = (
        float(proximity) if type(proximity) in (int, float) and math.isfinite(proximity) else 0.0
    )
    statement = insert(Foreshadow).values(id=foreshadow_id, project_id=project_id, **source)
    projected = (await db.execute(statement.on_conflict_do_update(index_elements=['id'],
        set_={key: getattr(statement.excluded, key) for key in source},
        where=and_(Foreshadow.project_id == project_id, Foreshadow.source == 'neo4j'))
        .returning(*Foreshadow.__table__.columns))).mappings().one_or_none()
    if projected is None:
        raise ForeshadowProjectionConflict('foreshadow_owner_changed')
    return dict(projected)
