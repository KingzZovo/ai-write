"""Latest source-verified place observations, without derived end-date guesses."""
from copy import deepcopy
import json
from typing import Any
from uuid import UUID


LOCATION_FIELDS = ('location', '位置')


def _source_fields(row: dict[str, Any]) -> dict[str, Any]:
    index = row.get('global_idx')
    chapter_id, stamp = row.get('source_chapter_id'), row.get('source_hash')
    if (type(index) is not int or index <= 0
            or not isinstance(chapter_id, (str, UUID)) or not str(chapter_id).strip()
            or not isinstance(stamp, str) or not stamp.strip()):
        raise ValueError('invalid_character_location_source')
    return dict(global_idx=index, source_chapter_id=str(chapter_id), source_hash=stamp)


def validate_location_owners(rows: list[dict[str, Any]], *, names: set[str] | None = None) -> None:
    """Check real graph owners before reducing their histories to name keys."""
    owners: dict[str, set[str]] = {}
    for row in rows:
        name = row.get('name')
        if names is not None and name not in names:
            continue
        if 'owner_node_id' not in row:
            continue  # Adapters/older callers may not expose a node identity.
        node = row['owner_node_id']
        if not isinstance(node, str) or not node.strip():
            raise ValueError('invalid_character_location_owner')
        owners.setdefault(name, set()).add(node)
    if any(len(nodes) > 1 for nodes in owners.values()):
        raise ValueError('ambiguous_character_location_owner')


def latest_location_records(rows: list[dict[str, Any]], *, identity: str = 'name') -> list[dict[str, Any]]:
    """Call after source verification; retain every distinct same-chapter place."""
    if identity not in ('name', 'character_id'):
        raise ValueError('invalid_character_location_identity')
    normalized, latest = [], {}
    if identity == 'name':
        validate_location_owners(rows)
    for row in rows:
        owner, place = row.get(identity), row.get('location')
        if (not isinstance(owner, str if identity == 'name' else (str, UUID)) or not str(owner).strip()
                or not isinstance(place, str) or not place.strip()):
            raise ValueError('invalid_character_location')
        if identity == 'character_id':
            owner = UUID(str(owner))
        source = _source_fields(row)
        normalized.append({identity: owner, 'location': place, **source})
        latest[owner] = max(latest.get(owner, 0), source['global_idx'])
    distinct = {}
    for row in normalized:
        if row['global_idx'] == latest[row[identity]]:
            key = (str(row[identity]), row['location'], row['source_chapter_id'], row['source_hash'])
            distinct[key] = row
    return [distinct[key] for key in sorted(distinct)]


def latest_location_state_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each exact field's latest verified values, including explicit unknowns.

    A same-chapter replica or second graph state cannot silently choose a
    place. Structured values retain their original shape and provenance.
    """
    normalized, latest = [], {}
    for row in rows:
        name, field = row.get('name'), row.get('field')
        if (not isinstance(name, str) or not name.strip() or field not in LOCATION_FIELDS
                or 'value' not in row):
            raise ValueError('invalid_character_location_state')
        value = deepcopy(row['value'])
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        source = _source_fields(row)
        normalized.append((dict(name=name, field=field, value=value, **source), encoded))
        key = (name, field)
        latest[key] = max(latest.get(key, 0), source['global_idx'])
    distinct = {}
    for row, encoded in normalized:
        if row['global_idx'] == latest[(row['name'], row['field'])]:
            key = (row['name'], row['field'], row['source_chapter_id'], row['source_hash'], encoded)
            distinct[key] = row
    return [distinct[key] for key in sorted(distinct)]


async def verified_character_locations(db, project_id, before_global_idx):
    from sqlalchemy import select
    from app.models.project import Character, CharacterLocation, Location
    from app.services.entity_sources import filter_current_entity_rows
    if type(before_global_idx) is not int or before_global_idx <= 0:
        return []
    pid = UUID(str(project_id))
    query = (select(CharacterLocation.character_id, Location.name, CharacterLocation.chapter_start,
                    CharacterLocation.source_chapter_id, CharacterLocation.source_hash)
        .join(Location, Location.id == CharacterLocation.location_id)
        .join(Character, Character.id == CharacterLocation.character_id)
        .where(CharacterLocation.project_id == pid, Location.project_id == pid, Character.project_id == pid,
               CharacterLocation.chapter_start < before_global_idx)
        .order_by(CharacterLocation.character_id, CharacterLocation.chapter_start.desc(), CharacterLocation.id)
        .execution_options(autoflush=False))
    rows = (await db.execute(query)).all()
    candidates = [dict(character_id=cid, location=place, global_idx=index,
                       source_chapter_id=sid, source_hash=stamp) for cid, place, index, sid, stamp in rows]
    valid = await filter_current_entity_rows(db, pid, candidates, before_global_idx=before_global_idx)
    return latest_location_records(valid, identity='character_id')
