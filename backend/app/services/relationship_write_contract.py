"""Author commands and exact graph snapshots; no literary identity inference."""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID

from app.services.rel_type import canonicalize_rel_type


class RelationshipWriteConflict(ValueError):
    pass


class RelationshipWriteUnconfirmed(RuntimeError):
    pass


class RelationshipSyncPending(RuntimeError):
    pass


class RelationshipProjectUnavailable(ValueError):
    pass


METADATA = {'label': '', 'note': '', 'sentiment': 'neutral'}


def frozen_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(frozen_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class RelationshipWriteCommand:
    id: str
    project_id: str
    source: str
    target: str
    rel_type: str
    operation: str
    expected_version: str | None
    changes: dict
    sha256: str


def make_relationship_command(request_id, project_id, source, target, rel_type,
                              operation, expected_version, changes):
    rid, pid = (str(UUID(str(value))) for value in (request_id, project_id))
    names = [value.strip() if isinstance(value, str) else '' for value in (source, target, rel_type)]
    if any(not value for value in names) or any(len(value) > limit for value, limit in zip(names, (200, 200, 100))):
        raise ValueError('invalid_relationship_identity')
    source, target, raw_type = names
    rel_type = canonicalize_rel_type(raw_type)
    if operation not in ('create', 'update', 'delete'):
        raise ValueError('invalid_relationship_operation')
    if operation != 'create' and (not isinstance(expected_version, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_version)):
        raise ValueError('missing_relationship_version')
    if operation == 'create' and expected_version is not None:
        raise ValueError('unexpected_relationship_version')
    allowed = set(METADATA) | ({'chapter_start'} if operation == 'create' else {'new_rel_type'})
    if not isinstance(changes, dict) or set(changes) - allowed or (operation == 'delete' and changes):
        raise ValueError('invalid_relationship_patch')
    if operation == 'update' and not changes:
        raise ValueError('empty_relationship_patch')
    patch = json.loads(frozen_json(changes))
    for key, default in METADATA.items():
        if key in patch:
            patch[key] = default if patch[key] is None else patch[key]
            if not isinstance(patch[key], str) or (key in ('label', 'sentiment') and len(patch[key]) > (200 if key == 'label' else 20)):
                raise ValueError('invalid_relationship_metadata')
    if 'new_rel_type' in patch:
        value = patch['new_rel_type']
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
            raise ValueError('invalid_relationship_type')
        patch['raw_type'] = value.strip()
        patch['type'] = canonicalize_rel_type(patch.pop('new_rel_type'))
    if operation == 'create':
        idx = patch.setdefault('chapter_start', 0)
        if type(idx) is not int or idx < 0:
            raise ValueError('invalid_relationship_chapter')
        patch.update(type=rel_type, raw_type=raw_type)
    data = dict(project_id=pid, source=source, target=target, rel_type=rel_type,
                operation=operation, expected_version=expected_version, changes=patch)
    return RelationshipWriteCommand(rid, **data, sha256=fingerprint(data))


def relationship_snapshot(project_id, source, target, rel_type, rows):
    key = canonicalize_rel_type(rel_type)
    entries = []
    for row in rows:
        props = row.get('properties')
        if not isinstance(props, dict) or not row.get('edge_id'):
            raise RelationshipWriteConflict('invalid_relationship_record')
        if not isinstance(props.get('type'), str) or not props['type'].strip():
            raise RelationshipWriteConflict('invalid_relationship_record')
        if canonicalize_rel_type(props['type']) != key:
            continue
        # The version and history cover every stored property, including legacy
        # provenance and extension fields that this editor does not change.
        entries.append({'edge_id': str(row['edge_id']), 'properties': dict(props)})
    entries.sort(key=lambda row: row['edge_id'])
    identity = dict(project_id=str(project_id), source=source, target=target, rel_type=key, entries=entries)
    fields, mixed = {}, []
    for name, default in METADATA.items():
        supplied = [row['properties'].get(name) for row in entries]
        if any(value is not None and not isinstance(value, str) for value in supplied):
            raise RelationshipWriteConflict('invalid_relationship_metadata')
        values = {default if value is None else value for value in supplied}
        fields[name] = next(iter(values)) if len(values) == 1 else default
        if len(values) > 1:
            mixed.append(name)
    try:
        version = fingerprint(identity)
    except (TypeError, ValueError) as exc:
        raise RelationshipWriteConflict('invalid_relationship_record') from exc
    return {**identity, 'version': version, 'fields': fields, 'mixed_fields': mixed}


def relationship_suppression_key(project_id, source, target, props):
    """Suppress only this exact extracted occurrence, not future plot events."""
    chapter, source_hash = props.get('source_chapter_id'), props.get('source_hash')
    if chapter and source_hash:
        return fingerprint([str(project_id), source, target, str(chapter), source_hash, props['type']])
    outline, digest = props.get('source_outline_id'), props.get('source_outline_digest')
    if outline and digest:
        return fingerprint(['outline', str(project_id), source, target, str(outline), digest, props['type']])
    return None
