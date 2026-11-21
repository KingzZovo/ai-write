"""Reconstruct and render source-verified, directed relationship records."""
from app.services.entity_timeline import RelationshipSnapshot
from app.services.relationship_identity import relationship_record_id, relationship_wording, valid_replacement_ids
from itertools import groupby
from uuid import UUID


def fold_relationship_events(rows: list[dict], project_id) -> list[RelationshipSnapshot]:
    """Rebuild intervals using exact references after source verification.

    New parallel types coexist. A change ends only referenced prior records;
    missing legacy scope leaves earlier roles pending, never implicitly ended.
    Stored end dates are derived from other chapters and cannot establish
    validity before those chapters' sources have been checked.
    """
    events = []
    for row in rows:
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ('name', 'target', 'rel_type', 'source_hash')):
            raise ValueError('invalid_relationship_context_record')
        if (type(row.get('global_idx')) is not int or row['global_idx'] <= 0
                or not row.get('source_chapter_id') or row.get('event_kind') not in ('new', 'change')):
            raise ValueError('relationship_context_provenance_unavailable')
        if row.get('raw_type') is not None and not isinstance(row['raw_type'], str):
            raise ValueError('invalid_relationship_context_description')
        rid = relationship_record_id(project_id, row['name'], row['target'], row['rel_type'],
                                     row['source_chapter_id'], row['source_hash'], row.get('raw_type'))
        if row.get('record_id') is not None and row['record_id'] != rid:
            raise ValueError('relationship_record_identity_changed')
        if row.get('replaces') is not None and not valid_replacement_ids(row['replaces']):
            raise ValueError('invalid_relationship_replacement_scope')
        if row.get('evidence') is not None and not isinstance(row['evidence'], str):
            raise ValueError('invalid_relationship_evidence')
        events.append(dict(row, record_id=rid, unresolved_replaces=[], scope_pending=None,
                           role_key=(row['name'], row['target'], row['rel_type'],
                                     relationship_wording(row['rel_type'], row.get('raw_type')))))

    latest: dict[tuple[str, str, str, str], dict] = {}
    active: dict[str, dict] = {}
    unknown_scopes: dict[tuple[str, str], dict] = {}

    def remove(rid):
        older = active.pop(rid)
        latest.pop(older['role_key'])

    # Events from one chapter are simultaneous. Two new roles can replace the
    # same older occurrence without treating the second reference as missing.
    for _, chapter in groupby(sorted(events, key=lambda row: (row['global_idx'], row['record_id'])),
                              key=lambda row: row['global_idx']):
        chapter = list(chapter)
        closed = set()
        for row in chapter:
            if row['event_kind'] != 'change':
                continue
            pair = (row['name'], row['target'])
            if row.get('replaces') is None:
                unknown_scopes[pair] = row
            for rid in row.get('replaces') or []:
                older = active.get(rid)
                if older and (older['name'], older['target']) == pair and older['global_idx'] < row['global_idx']:
                    closed.add(rid)
                else:
                    row['unresolved_replaces'].append(rid)
        for rid in closed:
            remove(rid)
        for row in chapter:
            older = latest.get(row['role_key'])
            if older:
                remove(older['record_id'])
            latest[row['role_key']] = row
            active[row['record_id']] = row
    # Only the latest unknown scope for a pair can affect a surviving earlier
    # role. Apply it once, without rescanning every active role for each chapter.
    for row in latest.values():
        scope = unknown_scopes.get((row['name'], row['target']))
        if scope and scope['global_idx'] > row['global_idx']:
            row['scope_pending'] = {key: scope[key] for key in (
                'global_idx', 'source_chapter_id', 'source_hash', 'record_id', 'rel_type')}
            row['scope_pending']['evidence'] = scope.get('evidence') or ''
    return [RelationshipSnapshot(
        row['name'], row['target'], row['rel_type'], row['global_idx'], None,
        source_chapter_id=str(row['source_chapter_id']), source_hash=row['source_hash'],
        event_kind=row['event_kind'], raw_type=row.get('raw_type') or '',
        record_id=row['record_id'], evidence=row.get('evidence') or '', replaces=row.get('replaces'),
        unresolved_replaces=row['unresolved_replaces'], scope_pending=row['scope_pending'],
    ) for _, row in sorted(latest.items())]


def render_relationship_record(relationship: RelationshipSnapshot) -> str:
    """Keep the registered direction and full wording, never infer its inverse."""
    if relationship.origin == 'author':
        UUID(relationship.author_id or '')
        UUID(relationship.author_edit_id or '')
        if type(relationship.author_updated_at) is not int or relationship.author_updated_at <= 0:
            raise ValueError('relationship_author_provenance_unavailable')
        description = relationship.raw_type if relationship.raw_type.strip() else relationship.rel_type
        details = [f'{label}：{value}' for label, value in (
            ('标签', relationship.label), ('备注', relationship.note), ('情感倾向', relationship.sentiment)) if value]
        return (f'- [作者设定] {relationship.source} → {relationship.target}：{description}'
                + ('；' + '；'.join(details) if details else ''))
    if (type(relationship.chapter_start) is not int or relationship.chapter_start <= 0
            or not relationship.source_chapter_id or not relationship.source_hash
            or relationship.event_kind not in ('new', 'change')):
        raise ValueError('relationship_context_provenance_unavailable')
    notes = []
    if relationship.event_kind == 'change':
        notes.append('变化记录')
        if relationship.replaces is None:
            notes.append('旧记录未标明结束范围，前后关系待核对')
        elif relationship.unresolved_replaces:
            notes.append('所指旧记录已不在有效前文中，结束范围待核对')
        elif not relationship.replaces:
            notes.append('未明确结束其他已登记关系')
    if relationship.scope_pending:
        notes.append(f'CH-{relationship.scope_pending["global_idx"]}发生变化后，本项是否仍成立待核对')
    change = '（' + '；'.join(notes) + '）' if notes else ''
    description = relationship.raw_type if relationship.raw_type.strip() else relationship.rel_type
    text = (f'- [CH-{relationship.chapter_start}] {relationship.source} → {relationship.target}：'
            f'{description}{change}' + (f'\n  原句：{relationship.evidence}' if relationship.evidence else ''))
    if relationship.scope_pending and relationship.scope_pending.get('evidence'):
        text += f'\n  待核对变化原句[CH-{relationship.scope_pending["global_idx"]}]：{relationship.scope_pending["evidence"]}'
    return text


def relationship_reference_candidates(relationships):
    """Detached model input; author settings are never automatic end targets."""
    return [dict(record_id=rel.record_id, char1=rel.source, char2=rel.target,
                 type=relationship_wording(rel.rel_type, rel.raw_type),
                 source_chapter_id=rel.source_chapter_id, source_hash=rel.source_hash,
                 global_idx=rel.chapter_start, evidence=rel.evidence,
                 scope_pending=rel.scope_pending)
            for rel in relationships if rel.origin != 'author']


def author_relationship_snapshot(row: dict) -> RelationshipSnapshot:
    for key in ('name', 'target', 'rel_type'):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError('invalid_author_relationship_record')
    fields = {}
    for key in ('raw_type', 'label', 'note', 'sentiment'):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError('invalid_author_relationship_metadata')
        fields[key] = value or ''
    result = RelationshipSnapshot(row['name'], row['target'], row['rel_type'], origin='author',
        author_id=row.get('author_id'), author_edit_id=row.get('author_edit_id'),
        author_updated_at=row.get('author_updated_at'), **fields)
    render_relationship_record(result)
    return result


def render_relationship_aliases(aliases: dict[str, list[str]]) -> str:
    """Show profile candidates alongside unchanged relationship endpoints."""
    if not aliases:
        return ''
    lines = ['【档案称呼核对】', '关系端点保留原称呼；以下只列档案对应，仍须结合原文确认身份。']
    for alias, names in sorted(aliases.items()):
        note = '多人登记，不能指定其中一人' if len(names) > 1 else '档案登记别名'
        lines.append(f'- {alias}：{"、".join(names)}（{note}）')
    return '\n'.join(lines)
