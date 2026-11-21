"""Project graph relationships and explicit metadata without replacing history."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from app.models.project import Character, Relationship
from app.services.entity_bulk import upsert_projection_rows
from app.services.rel_type import canonicalize_rel_type
from app.services.relationship_write_contract import METADATA, RelationshipWriteConflict
from app.services.relationship_graph_write import read_pair, require_relationship_pair_ready


def relationship_projection_rows(project_id, rows, name_to_id, canonical=lambda name: name):
    """Keep the latest supplied value per field; absent values do not clear PG.

    The read table groups types using its existing canonical key. Full wording
    and chapter/author provenance remain in the graph and writing context.
    """
    groups = {}
    for row in rows:
        src, tgt, props = canonical(row['source']), canonical(row['target']), row['properties']
        if src not in name_to_id or tgt not in name_to_id:
            raise RelationshipWriteConflict('relationship_character_unavailable')
        key = (src, tgt, canonicalize_rel_type(props['type']))
        group = groups.setdefault(key, {'records': [], 'ids': set()})
        group['records'].append((row.get('edge_id', ''), props))
        if props.get('projection_id'):
            group['ids'].add(str(UUID(props['projection_id'])))
    projected = []
    for (src, tgt, kind), group in sorted(groups.items()):
        if len(group['ids']) > 1:
            raise RelationshipWriteConflict('ambiguous_relationship_identity')
        values = dict(project_id=UUID(str(project_id)), source_id=UUID(str(name_to_id[src])),
                      target_id=UUID(str(name_to_id[tgt])), rel_type=kind)
        if group['ids']:
            values['id'] = UUID(next(iter(group['ids'])))
        def rank(item):
            edge_id, props = item
            author = props.get('origin') == 'author'
            position = props.get('author_updated_at') if author else props.get('chapter_start')
            return (author, position if type(position) is int else -1, str(edge_id))
        for _, props in sorted(group['records'], key=rank):
            for field, default in METADATA.items():
                if field in props:
                    value = props[field] if props[field] is not None else default
                    if not isinstance(value, str):
                        raise RelationshipWriteConflict('invalid_relationship_metadata')
                    values[field] = value
        projected.append(values)
    return projected


async def upsert_relationship_projection(db, rows):
    """Caller owns the materialization mutex and transaction.

    Author records carry a stable projection ID through type changes, so
    evolution_json, volume bounds, creation time and external references stay.
    """
    # Count actual creations, not metadata updates or a type change that keeps
    # the same ID. Bound the lookup just like the bulk write, also for long books.
    existing_by_id, existing_by_key = {}, {}
    for start in range(0, len(rows), 500):
        batch = rows[start:start + 500]
        keys = [(r['project_id'], r['source_id'], r['target_id'], r['rel_type']) for r in batch]
        ids = [r['id'] for r in batch if r.get('id')]
        existing = (await db.execute(select(Relationship.id, Relationship.project_id,
            Relationship.source_id, Relationship.target_id, Relationship.rel_type).where(or_(
                tuple_(Relationship.project_id, Relationship.source_id, Relationship.target_id,
                       Relationship.rel_type).in_(keys), Relationship.id.in_(ids)))
            .with_for_update().execution_options(autoflush=False))).all()
        existing_by_id.update((row[0], tuple(row[1:])) for row in existing)
        existing_by_key.update((tuple(row[1:]), row[0]) for row in existing)
    batches, renames = {}, []
    for row in rows:
        values = dict(row)
        key = (values['project_id'], values['source_id'], values['target_id'], values['rel_type'])
        if values.get('id'):
            existing = existing_by_id.get(values['id'])
            if existing is not None and existing[:3] != key[:3]:
                raise RelationshipWriteConflict('relationship_projection_owner_changed')
            occupant = existing_by_key.get(key)
            if occupant is not None and occupant != values['id']:
                raise RelationshipWriteConflict('relationship_projection_identity_changed')
            if existing is not None and existing != key:
                renames.append((values['id'], existing, key))
        fields = tuple(field for field in METADATA if field in values)
        # Separate rows with and without an explicit ID as well as field sets.
        batches.setdefault((fields, 'id' in values), []).append(values)
    # All ownership/collision checks finish before the first mutation. Ordinary
    # chapter syncs do no per-author SELECTs or unchanged-type UPDATEs.
    current_keys = set(existing_by_key)
    for rid, before, after in renames:
        await db.execute(update(Relationship).where(Relationship.id == rid)
            .values(rel_type=after[-1]).execution_options(synchronize_session=False))
        current_keys.discard(before)
        current_keys.add(after)
    created = sum(row.get('id') not in existing_by_id and
        (row['project_id'], row['source_id'], row['target_id'], row['rel_type']) not in current_keys
        for row in rows)
    for (fields, _), batch in batches.items():
        await upsert_projection_rows(db, Relationship, batch,
            constraint='uq_relationships_rel_key', update_columns=fields)
    return created


async def relationship_projection_basis(db, project_id, source, target, rel_type):
    """Lock and preserve the original read row, including volume history on delete."""
    characters = dict((await db.execute(select(Character.name, Character.id).where(
        Character.project_id == UUID(str(project_id)), Character.name.in_([source, target]))
        .execution_options(autoflush=False))).all())
    if source in characters and target in characters:
        existing = (await db.execute(select(*Relationship.__table__.columns).where(
            Relationship.project_id == UUID(str(project_id)), Relationship.source_id == characters[source],
            Relationship.target_id == characters[target], Relationship.rel_type == rel_type)
            .with_for_update().execution_options(autoflush=False))).mappings().one_or_none()
        if existing is not None:
            snapshot = {key: value.isoformat() if isinstance(value, datetime) else
                        str(value) if isinstance(value, UUID) else value for key, value in existing.items()}
            return str(existing['id']), snapshot
    return str(uuid4()), None


async def project_relationship_pair(db, driver, project_id, source, target):
    """Read current graph values, never replay an earlier receipt's patch."""
    project_id = UUID(str(project_id))
    async with driver.session() as session:
        await require_relationship_pair_ready(session, project_id, source, target)
        graph = await read_pair(session, project_id, source, target)
    names = dict((await db.execute(select(Character.name, Character.id).where(
        Character.project_id == project_id, Character.name.in_([source, target]))
        .execution_options(autoflush=False))).all())
    if graph:
        for name in dict.fromkeys((source, target)):
            if name not in names:
                await db.execute(insert(Character).values(project_id=project_id, name=name, profile_json={})
                    .on_conflict_do_nothing(constraint='uq_characters_project_id_name'))
        names = dict((await db.execute(select(Character.name, Character.id).where(
            Character.project_id == project_id, Character.name.in_([source, target]))
            .execution_options(autoflush=False))).all())
        rows = relationship_projection_rows(project_id,
            [dict(row, source=source, target=target) for row in graph], names)
        await upsert_relationship_projection(db, rows)
    else:
        rows = []
    if source in names and target in names:
        statement = delete(Relationship).where(Relationship.project_id == project_id,
            Relationship.source_id == names[source], Relationship.target_id == names[target])
        if rows:
            statement = statement.where(Relationship.rel_type.not_in([row['rel_type'] for row in rows]))
        await db.execute(statement.execution_options(synchronize_session=False))
    return graph
