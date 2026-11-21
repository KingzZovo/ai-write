"""Validated identities and fingerprints shared by author writes and recovery."""
import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID


class ForeshadowWriteConflict(ValueError):
    def __init__(self, code='source_changed'):
        self.code = code
        super().__init__(code)


class ForeshadowProjectUnavailable(ValueError):
    pass


class ForeshadowSyncPending(RuntimeError):
    pass


class ForeshadowWriteUnconfirmed(RuntimeError):
    pass


WRITE_FIELDS = frozenset({
    'type', 'description', 'planted_chapter', 'resolve_conditions_json',
    'resolution_blueprint_json', 'narrative_proximity', 'status', 'resolved_chapter',
})


@dataclass(frozen=True)
class ForeshadowWriteCommand:
    id: UUID
    project_id: UUID
    foreshadow_id: UUID
    operation: str
    expected_version: str | None
    changes: dict
    sha256: str


def make_write_command(request_id, project_id, foreshadow_id, operation, expected_version, changes):
    if operation not in {'create', 'update', 'delete', 'resolve'}:
        raise ValueError('invalid_foreshadow_operation')
    if operation != 'create' and (not isinstance(expected_version, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_version)):
        raise ValueError('missing_foreshadow_version')
    if operation == 'create' and expected_version is not None:
        raise ValueError('unexpected_foreshadow_version')
    if not isinstance(changes, dict) or set(changes) - WRITE_FIELDS:
        raise ValueError('invalid_foreshadow_patch')
    # Freeze JSON values before IO; retries bind to exactly this patch.
    changes = json.loads(json.dumps(changes, ensure_ascii=False, allow_nan=False))
    for key, default in (('resolve_conditions_json', []), ('resolution_blueprint_json', {})):
        if key in changes and changes[key] is None:
            changes[key] = default
    rid, pid, fid = (UUID(str(value)) for value in (request_id, project_id, foreshadow_id))
    raw = json.dumps({'project_id': str(pid), 'foreshadow_id': str(fid), 'operation': operation,
        'expected_version': expected_version, 'changes': changes}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    return ForeshadowWriteCommand(rid, pid, fid, operation, expected_version, changes,
        hashlib.sha256(raw.encode('utf-8')).hexdigest())


def receipt_for(command, source, outcome, reason=None):
    return dict(id=command.id, project_id=command.project_id, foreshadow_id=command.foreshadow_id,
        command_sha256=command.sha256, operation=command.operation, source=source,
        outcome=outcome, reason=reason)


def require_same_receipt(receipt, expected):
    fields = ('id', 'project_id', 'foreshadow_id', 'command_sha256', 'operation')
    if any(str(receipt[key]) != str(expected[key]) for key in fields):
        raise ForeshadowWriteConflict('request_id_reused')
