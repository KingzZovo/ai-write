"""Source-verified chapter-derived graph facts for generation contexts."""
import json
from uuid import UUID
from app.services.character_locations import (
    LOCATION_FIELDS, latest_location_records, latest_location_state_records, validate_location_owners,
)
from app.services.entity_sources import filter_current_entity_rows
from app.services.entity_timeline import CharacterSnapshot, WorldSnapshot
from app.services.relationship_context import (
    author_relationship_snapshot, fold_relationship_events, relationship_reference_candidates,
)


RELATIONSHIP_MATCH = ('MATCH (c:Character {project_id:$pid})-[e:RELATES_TO]->'
                      '(n:Character {project_id:$pid}) ')
RELATIONSHIP_FIELDS = (
    'c.name AS name,n.name AS target,e.type AS rel_type,e.event_kind AS event_kind,e.raw_type AS raw_type,'
    'e.origin AS origin,e.author_id AS author_id,e.author_edit_id AS author_edit_id,'
    'e.author_updated_at AS author_updated_at,e.label AS label,e.note AS note,e.sentiment AS sentiment,'
    'e.record_id AS record_id,e.evidence AS evidence,e.replaces AS replaces')
SOURCE_FIELDS = (',e.source_chapter_id AS source_chapter_id,e.source_hash AS source_hash,'
                 'e.chapter_start AS global_idx,e.chapter_end AS chapter_end ')


def _with_relationship_evidence(rows):
    # Older projections did not retain quotes. New quotes must still occur in
    # the verified full source, just like other entity evidence excerpts.
    return [dict(row, excerpt=row['evidence']) if row.get('kind') == 'relationship'
            and row.get('evidence') else row for row in rows]


async def verified_relationship_candidates(db, driver, project_id, before_global_idx, *, pairs=None):
    """Read complete history for exact directed pairs, then verify its sources.

    Omitted pairs retain the all-project interface. An empty selection needs no
    graph IO; names, aliases and relationship meanings are never inferred here.
    """
    if type(before_global_idx) is not int or before_global_idx <= 0:
        return []
    if pairs is not None:
        pairs = list(pairs)
        if any(not isinstance(pair, (list, tuple)) or len(pair) != 2
               or any(not isinstance(name, str) or not name.strip() for name in pair) for pair in pairs):
            raise ValueError('invalid_relationship_candidate_pairs')
        pairs = sorted({tuple(pair) for pair in pairs})
        if not pairs:
            return []
    async def read(tx):
        batches = [None] if pairs is None else [pairs[start:start + 200] for start in range(0, len(pairs), 200)]
        rows = []
        for batch in batches:
            match = RELATIONSHIP_MATCH if batch is None else (
                'UNWIND $pairs AS pair '
                'MATCH (c:Character {project_id:$pid,name:pair[0]})-[e:RELATES_TO]->'
                '(n:Character {project_id:$pid,name:pair[1]}) ')
            result = await tx.run(match +
                'WHERE e.source_chapter_id IS NOT NULL AND e.chapter_start<$idx '
                'AND coalesce(e.origin,"")<>"author" RETURN ' + RELATIONSHIP_FIELDS + SOURCE_FIELDS +
                'ORDER BY name,e.chapter_start DESC', pid=str(project_id), idx=before_global_idx,
                **({'pairs': batch} if batch is not None else {}))
            rows.extend([dict(row, kind='relationship') async for row in result])
        return rows
    async with driver.session() as session:
        rows = await session.execute_read(read)
    rows = await filter_current_entity_rows(db, project_id, _with_relationship_evidence(rows),
                                           before_global_idx=before_global_idx)
    return relationship_reference_candidates(fold_relationship_events(rows, project_id))


async def verified_world_snapshot(db, driver, project_id, before_global_idx, *, include_profiles=False):
    snapshot = WorldSnapshot()
    if type(before_global_idx) is not int or before_global_idx <= 0:
        return snapshot
    queries = (
        ('state', 'MATCH (c:Character {project_id:$pid})-[:HAS_STATE]->(e:CharacterState) ',
         'c.name AS name,elementId(c) AS owner_node_id,e.status_json AS status'),
        ('relationship', RELATIONSHIP_MATCH, RELATIONSHIP_FIELDS),
        ('location', 'MATCH (c:Character {project_id:$pid})-[e:AT_LOCATION]->(n:Location {project_id:$pid}) ',
         'c.name AS name,elementId(c) AS owner_node_id,n.name AS location'),
    )
    async def read(tx):
        rows = []
        if include_profiles:
            result = await tx.run('MATCH (c:Character {project_id:$pid}) '
                'RETURN elementId(c) AS node_id,c.id AS id,c.name AS name,'
                'c.profile_json AS profile_json,c.profile_origin AS profile_origin ORDER BY name,node_id',
                pid=str(project_id))
            rows.extend([dict(record, kind='profile') async for record in result])
        for kind, match, fields in queries:
            temporal = 'AND e.source_mode="chapter_delta" ' if kind == 'state' else ''
            # States need earlier field updates. Relationships must rebuild
            # derived intervals only after checking the closing source too.
            # Location end dates are also derived from other chapters. Check
            # the closing source before allowing it to hide an older location.
            # No history may be silently cut off by a global row limit.
            source_filter = ('WHERE ((e.source_chapter_id IS NOT NULL AND e.chapter_start<$idx) OR e.origin="author") '
                if kind == 'relationship' else 'WHERE e.source_chapter_id IS NOT NULL AND e.chapter_start<$idx ')
            result = await tx.run(match +
                source_filter + temporal +
                'RETURN ' + fields + SOURCE_FIELDS +
                'ORDER BY name,e.chapter_start DESC',
                pid=str(project_id), idx=before_global_idx)
            rows.extend([dict(record, kind=kind) async for record in result])
        return rows
    async with driver.session() as session:
        candidates = await session.execute_read(read)
    snapshot.profiles = [{key: row.get(key) for key in ('node_id', 'id', 'name', 'profile_json', 'profile_origin')}
                         for row in candidates if row['kind'] == 'profile']
    author_rows = [row for row in candidates if row['kind'] == 'relationship' and row.get('origin') == 'author']
    rows = await filter_current_entity_rows(db, project_id,
        _with_relationship_evidence([row for row in candidates if row['kind'] != 'profile' and row.get('origin') != 'author']),
        before_global_idx=before_global_idx)
    folded = fold_state_changes([row for row in rows if row['kind'] == 'state'], strict=include_profiles)
    for name, state in folded.items():
        snapshot.characters.append(CharacterSnapshot(name, state['status'], state['global_idx'], None,
                                                       field_sources=state['field_sources']))
        snapshot.location_state_records.extend(state.get('location_states', []))
    snapshot.relationships = fold_relationship_events([row for row in rows if row['kind'] == 'relationship'], project_id)
    snapshot.relationships.extend(author_relationship_snapshot(row) for row in author_rows)
    located_names = {row['name'] for row in rows if row['kind'] == 'location'}
    located_names.update(row['name'] for row in snapshot.location_state_records)
    validate_location_owners([row for row in rows if row['kind'] in ('state', 'location')], names=located_names)
    snapshot.location_records = latest_location_records([row for row in rows if row['kind'] == 'location'])
    snapshot.locations = list(dict.fromkeys(row['location'] for row in snapshot.location_records))
    return snapshot


def explicitly_dead(status):
    """Death warnings require an explicit state, not a substring in prose."""
    if isinstance(status, str):
        try: status = json.loads(status)
        except (ValueError, TypeError): return False
    if not isinstance(status, dict):
        return False
    return (status.get('alive') is False or status.get('status') == 'dead'
            or any(status.get(key) in ('死亡', '已死亡', '已死', '身亡')
                   for key in ('状态', '生死')))


def _unique_state_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_character_state_field')
        result[key] = value
    return result


def fold_state_changes(rows, *, strict=False):
    """Accumulate verified chapter deltas, keeping each field's actual source.

    Newest value per exact field wins. Explicit null is retained as an unknown
    value, so an older value cannot silently reappear through dictionary merge.
    """
    states, location_states = {}, []
    for row in sorted(rows, key=lambda r: (r['global_idx'], str(r.get('source_chapter_id'))), reverse=True):
        name, status = row.get('name'), row.get('status')
        if not name:
            if strict: raise ValueError('invalid_character_state_name')
            continue
        if isinstance(status, str):
            try: status = json.loads(status, object_pairs_hook=_unique_state_fields if strict else None)
            except (ValueError, TypeError):
                if strict: raise ValueError('invalid_character_state_payload') from None
                continue
        if not isinstance(status, dict):
            if strict: raise ValueError('invalid_character_state_payload')
            continue
        if strict:
            json.dumps(status, allow_nan=False)
            if not status:
                continue  # An empty delta carries no field update or receipt.
        state = states.setdefault(name, {'status': {}, 'field_sources': {}, 'global_idx': row['global_idx']})
        for key, value in status.items():
            if (strict and key in state['status'] and state['field_sources'][key]['global_idx'] == row['global_idx']
                    and json.dumps(value, sort_keys=True) != json.dumps(state['status'][key], sort_keys=True)):
                raise ValueError('ambiguous_character_state_field')
            if key not in state['status']:
                state['status'][key] = value
                state['field_sources'][key] = {field: row[field] for field in
                                               ('source_chapter_id', 'source_hash', 'global_idx')}
            if key in LOCATION_FIELDS:
                location_states.append(dict(name=name, field=key, value=value,
                    **{field: row[field] for field in ('source_chapter_id', 'source_hash', 'global_idx')}))
    for record in latest_location_state_records(location_states):
        states[record['name']].setdefault('location_states', []).append(record)
    return states


async def verified_character_states(db, project_id, before_global_idx, names=None):
    from sqlalchemy import select
    from app.models.project import Character, CharacterState
    if type(before_global_idx) is not int or before_global_idx <= 0:
        return {}
    query = select(Character.name, CharacterState.status_json, CharacterState.chapter_start,
                   CharacterState.source_chapter_id, CharacterState.source_hash).join(
                       Character, Character.id == CharacterState.character_id).where(
        CharacterState.project_id == UUID(str(project_id)), Character.project_id == UUID(str(project_id)),
        CharacterState.chapter_start < before_global_idx,
        CharacterState.source_mode == 'chapter_delta')
    if names is not None:
        query = query.where(Character.name.in_(names))
    rows = (await db.execute(query.execution_options(autoflush=False))).all()
    candidates = [dict(name=name, status=status, global_idx=idx, source_chapter_id=sid, source_hash=shash)
                  for name, status, idx, sid, shash in rows]
    valid = await filter_current_entity_rows(db, project_id, candidates, before_global_idx=before_global_idx)
    return fold_state_changes(valid)
