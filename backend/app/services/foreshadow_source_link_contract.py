"""Exact source identity for author associations, without literary scoring."""
import json
import re
from dataclasses import dataclass
from uuid import UUID

from app.services.entity_sources import digest
from app.services.foreshadow_discovery import source_snapshot
from app.services.foreshadow_scope import positive_index


class SourceLinkConflict(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SourceLinkNotFound(ValueError):
    pass


def record_snapshot(record):
    # Changes to payoff plans/status do not change the identity of the planted
    # clue. They remain protected by the full If-Match version when linking.
    return {key: record[key] for key in ('type', 'description', 'planted_chapter', 'source')}


def linked_source_snapshot(source):
    snapshot = source_snapshot(source)
    volume_idx = source.receipt.get('volume_idx')
    if not positive_index(volume_idx):
        raise SourceLinkConflict('chapter_unavailable')
    return {**snapshot, 'volume_idx': volume_idx}


def locate_quote(content, evidence):
    if not isinstance(evidence, str) or not evidence.strip():
        raise SourceLinkConflict('quote_invalid')
    start = content.find(evidence)
    if start < 0:
        raise SourceLinkConflict('quote_missing')
    if content.find(evidence, start + 1) >= 0:
        raise SourceLinkConflict('quote_ambiguous')
    return start, start + len(evidence)


@dataclass(frozen=True)
class SourceLinkCommand:
    id: UUID
    project_id: UUID
    foreshadow_id: UUID
    expected_version: str
    operation: str
    payload: dict
    sha256: str


def make_source_link_command(request_id, project_id, foreshadow_id, expected_version, operation, payload):
    if operation not in {'link', 'unlink'} or not re.fullmatch('[0-9a-f]{64}', expected_version or ''):
        raise ValueError('invalid_source_link_command')
    required = {'expected_link_id', 'note'}
    if operation == 'link':
        required |= {'chapter_id', 'source_version', 'evidence'}
    if not isinstance(payload, dict) or set(payload) != required or not isinstance(payload['note'], str):
        raise ValueError('invalid_source_link_payload')
    payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    if payload['expected_link_id'] is not None:
        payload['expected_link_id'] = str(UUID(payload['expected_link_id']))
    if operation == 'link':
        payload['chapter_id'] = str(UUID(payload['chapter_id']))
        if not re.fullmatch('[0-9a-f]{64}', payload['source_version'] or ''):
            raise ValueError('invalid_source_version')
        if not isinstance(payload['evidence'], str) or not payload['evidence'].strip():
            raise SourceLinkConflict('quote_invalid')
    rid, pid, fid = [UUID(str(value)) for value in (request_id, project_id, foreshadow_id)]
    command_hash = digest(dict(project_id=str(pid), foreshadow_id=str(fid),
        expected_version=expected_version, operation=operation, payload=payload))
    return SourceLinkCommand(rid, pid, fid, expected_version, operation, payload, command_hash)


def author_source_state(row, record, source):
    """Latest author intent is authoritative even when unlinked or stale."""
    if row['operation'] == 'unlink':
        return 'unknown'
    try:
        snapshot = row['source_snapshot']
        if (row['operation'] != 'link' or not isinstance(snapshot, dict)
                or digest(snapshot) != row['source_sha256']
                or digest(row['record_snapshot']) != row['record_sha256']
                or snapshot.get('project_id') != str(row['project_id'])
                or snapshot.get('chapter_id') != str(row['chapter_id'])):
            return 'unknown'
        if row['record_snapshot'] != record_snapshot(record):
            return 'changed'
        if source is None:
            return 'unavailable'
        if snapshot != linked_source_snapshot(source):
            return 'changed'
        span = locate_quote(source.content, row['evidence'])
        if (any(type(row[key]) is not int for key in ('evidence_start', 'evidence_end'))
                or span != (row['evidence_start'], row['evidence_end'])):
            return 'unknown'
        return 'current'
    except (KeyError, ValueError, TypeError, AttributeError, RuntimeError):
        return 'unknown'
