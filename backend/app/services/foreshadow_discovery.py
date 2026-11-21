"""Discover possible clues from full prose; persistence never promotes them to facts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.models.project import (ForeshadowDiscovery as Candidate, ForeshadowDiscoveryRun as Run,
    ForeshadowDiscoveryRunItem as RunItem, _utcnow)
from app.services.entity_sources import digest
from app.services.provider_response_guard import require_normal_finish

MAX_INPUT_CHARS = 24000
DISCOVERY_NAMESPACE = UUID('c030214e-33d2-4ea4-9a34-e85780162a2b')


class DiscoveryIncomplete(RuntimeError):
    """No reliable complete result exists; an empty list would be misleading."""


@dataclass(frozen=True)
class ClueCandidate:
    description: str
    type: str
    evidence: str
    evidence_start: int
    evidence_end: int
    reason: str


@dataclass(frozen=True)
class DiscoveryBatch:
    source_snapshot: dict
    source_sha256: str
    candidates: tuple[ClueCandidate, ...] = ()
    cached_count: int | None = None
    scan_revision: int = 0
    model_name: str | None = None


def source_snapshot(source) -> dict:
    """Content and persisted identity/position define this editorial source.

    A title edit or draft→completed transition does not change the cited
    prose. Restoring the exact same source reuses its prior author decisions.
    The worker separately fences the richer saved-source revision.
    """
    chapter_idx, global_idx = source.receipt['chapter_idx'], source.global_idx
    if (type(chapter_idx) is not int or not 0 < chapter_idx <= 2147483647
            or type(global_idx) is not int or not 0 < global_idx <= 2147483647
            or not isinstance(source.content, str) or not source.content.strip()):
        raise DiscoveryIncomplete('discovery_source_invalid')
    return {
        'version': 1, 'project_id': str(UUID(str(source.project_id))),
        'chapter_id': str(UUID(str(source.chapter_id))),
        'volume_id': str(UUID(str(source.receipt['volume_id']))),
        'chapter_idx': chapter_idx, 'global_idx': global_idx,
        'content_sha256': hashlib.sha256(source.content.encode()).hexdigest(),
    }


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_discovery_field')
        result[key] = value
    return result


def parse_candidates(raw: str, text: str) -> tuple[ClueCandidate, ...]:
    """Validate the entire result before any persistence, including all quotes."""
    if not isinstance(raw, str):
        raise ValueError('invalid_discovery_response')
    data = json.loads(raw.strip(), object_pairs_hook=_unique_keys)
    if not isinstance(data, list):
        raise ValueError('invalid_discovery_response')
    candidates, seen_spans, seen_descriptions = [], set(), set()
    for item in data:
        if not isinstance(item, dict) or set(item) != {'description', 'type', 'evidence', 'reason'}:
            raise ValueError('invalid_discovery_fields')
        if any(not isinstance(value, str) or not value.strip() for value in item.values()):
            raise ValueError('invalid_discovery_fields')
        if item['type'] not in {'plot', 'character', 'worldbuilding', 'mystery'}:
            raise ValueError('invalid_discovery_type')
        evidence = item['evidence']
        start = text.find(evidence)
        if start < 0 or text.find(evidence, start + 1) >= 0:
            raise ValueError('discovery_quote_not_unique')
        span, description = (start, start + len(evidence)), item['description'].strip()
        if span in seen_spans or description in seen_descriptions:
            raise ValueError('duplicate_discovery_candidate')
        seen_spans.add(span); seen_descriptions.add(description)
        candidates.append(ClueCandidate(description, item['type'], evidence, *span, item['reason'].strip()))
    return tuple(candidates)


async def detect_clues(source) -> DiscoveryBatch:
    """Registry routing is configurable; its actual prompt joins the budget."""
    from app.services.prompt_registry import run_text_prompt
    snapshot = source_snapshot(source)
    # Cheap rejection before opening configuration sessions; no partial prose.
    if len(source.content) > MAX_INPUT_CHARS:
        raise DiscoveryIncomplete('discovery_input_budget_exceeded')
    try:
        result = await run_text_prompt(
            'chapter_foreshadows', '待核查完整保存稿：\n' + source.content, db=None,
            project_id=source.project_id, chapter_id=source.chapter_id,
            temperature=0.2, max_tokens=4096, input_char_limit=MAX_INPUT_CHARS,
        )
        require_normal_finish(getattr(result, 'finish_reason', None))
        candidates = parse_candidates(result.text, source.content)
    except Exception as exc:
        raise DiscoveryIncomplete('discovery_incomplete') from exc
    # GenerationResult.model is the model name passed to the successful
    # provider call, including a router fallback. It is not proof of the
    # upstream implementation behind a CPA alias. Never re-read live config.
    model = getattr(result, 'model', None)
    model = model if isinstance(model, str) and model.strip() and len(model) <= 200 else None
    return DiscoveryBatch(snapshot, digest(snapshot), candidates, model_name=model)


def validate_scan_revision(revision):
    if type(revision) is not int or not 0 <= revision <= 9223372036854775807:
        raise DiscoveryIncomplete('discovery_scan_revision_invalid')


async def read_scan_members(db, run_ids):
    if not run_ids:
        return {}
    origin = aliased(Run)
    rows = (await db.execute(select(RunItem.run_id, RunItem.position, RunItem.reason, origin.source_snapshot,
        origin.source_sha256, origin.project_id, origin.chapter_id).select_from(RunItem)
        .outerjoin(Candidate, RunItem.candidate_id == Candidate.id).outerjoin(origin, Candidate.run_id == origin.id)
        .where(RunItem.run_id.in_(run_ids)).order_by(RunItem.run_id, RunItem.position)
        .execution_options(autoflush=False))).mappings().all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row['run_id'], []).append(row)
    return grouped


def complete_scan_receipt(row, items):
    """A declared zero or a partial/wrong-source member list is not enough."""
    try:
        validate_scan_revision(row['scan_revision'])
        snapshot = row['source_snapshot']
        if (not isinstance(snapshot, dict) or snapshot.get('version') != 1
                or snapshot.get('project_id') != str(row['project_id'])
                or snapshot.get('chapter_id') != str(row['chapter_id'])
                or digest(snapshot) != row['source_sha256'] or type(row['candidate_count']) is not int
                or row['candidate_count'] < 0 or len(items) != row['candidate_count']):
            return False
        return all(type(item['position']) is int and item['position'] == position
            and isinstance(item['reason'], str) and bool(item['reason'].strip())
            and item['source_snapshot'] == snapshot and item['source_sha256'] == row['source_sha256']
            and str(item['project_id']) == snapshot['project_id'] and str(item['chapter_id']) == snapshot['chapter_id']
            for position, item in enumerate(items))
    except (KeyError, ValueError, TypeError, DiscoveryIncomplete):
        return False


async def cached_discovery(db, source, *, scan_revision: int | None = None) -> DiscoveryBatch | None:
    """Ordinary work uses the latest success; a rescan only reuses its own run."""
    snapshot = source_snapshot(source)
    query = select(*Run.__table__.columns).where(
        Run.project_id == UUID(snapshot['project_id']), Run.chapter_id == UUID(snapshot['chapter_id']),
        Run.source_sha256 == digest(snapshot),
    )
    if scan_revision is not None:
        validate_scan_revision(scan_revision)
        query = query.where(Run.scan_revision == scan_revision)
    row = (await db.execute(query.order_by(Run.scan_revision.desc(), Run.id.desc()).limit(1)
        .execution_options(autoflush=False))).mappings().one_or_none()
    if row is None:
        return None
    members = await read_scan_members(db, [row['id']])
    if row['source_snapshot'] != snapshot or not complete_scan_receipt(row, members.get(row['id'], [])):
        raise DiscoveryIncomplete('discovery_receipt_invalid')
    return DiscoveryBatch(snapshot, digest(snapshot), cached_count=row['candidate_count'],
        scan_revision=row['scan_revision'], model_name=row['model_name'])


def _candidate_key(candidate):
    return tuple(candidate[key] for key in ('description', 'type', 'evidence', 'evidence_start', 'evidence_end'))


async def persist_discovery(db, batch: DiscoveryBatch, *, locked_source) -> int:
    """Caller holds the canonical chapter/volume lock and owns the transaction.

    The worker commits the receipt, candidates, payoff suggestions and task
    completion together. Another attempt's result wins as a whole; no merge
    of nondeterministic lists or resetting of author decisions is allowed.
    """
    snapshot = source_snapshot(locked_source)
    if batch.source_snapshot != snapshot or batch.source_sha256 != digest(snapshot):
        raise DiscoveryIncomplete('discovery_source_changed')
    validate_scan_revision(batch.scan_revision)
    existing = await cached_discovery(db, locked_source, scan_revision=batch.scan_revision)
    if existing is not None:
        return existing.cached_count
    if batch.cached_count is not None:
        raise DiscoveryIncomplete('discovery_receipt_missing')
    # Revalidate all item fields and offsets even for an internal caller.
    raw = json.dumps([{'description': c.description, 'type': c.type, 'evidence': c.evidence, 'reason': c.reason}
                      for c in batch.candidates], ensure_ascii=False)
    if parse_candidates(raw, locked_source.content) != batch.candidates:
        raise DiscoveryIncomplete('discovery_evidence_invalid')
    if batch.model_name is not None and (not isinstance(batch.model_name, str) or not batch.model_name.strip() or len(batch.model_name) > 200):
        raise DiscoveryIncomplete('discovery_model_label_invalid')
    run_id = uuid5(DISCOVERY_NAMESPACE, batch.source_sha256 if batch.scan_revision == 0
        else f'{batch.source_sha256}:{batch.scan_revision}')
    inserted = (await db.execute(insert(Run).values(
        id=run_id, project_id=UUID(snapshot['project_id']), chapter_id=UUID(snapshot['chapter_id']),
        source_sha256=batch.source_sha256, source_snapshot=snapshot,
        scan_revision=batch.scan_revision, model_name=batch.model_name,
        candidate_count=len(batch.candidates), created_at=_utcnow(),
    ).on_conflict_do_nothing(index_elements=['chapter_id', 'source_sha256', 'scan_revision'])
        .returning(Run.id))).scalar_one_or_none()
    if inserted is None:
        winner = await cached_discovery(db, locked_source, scan_revision=batch.scan_revision)
        if winner is None:
            raise DiscoveryIncomplete('discovery_receipt_missing')
        return winner.cached_count
    if batch.candidates:
        # Restrict reuse to the exact source and descriptions in this scan.
        # No similarity score, current Foreshadow contents, or old decision is
        # used to infer equivalence. The chapter lock serializes new identities.
        old = (await db.execute(select(*Candidate.__table__.columns, Run.source_snapshot)
            .join(Run, Candidate.run_id == Run.id).where(Run.project_id == UUID(snapshot['project_id']),
                Run.chapter_id == UUID(snapshot['chapter_id']), Run.source_sha256 == batch.source_sha256,
                Candidate.description.in_([c.description for c in batch.candidates]))
            .execution_options(autoflush=False))).mappings().all()
        identities = {}
        for candidate in old:
            if candidate['source_snapshot'] != snapshot:
                raise DiscoveryIncomplete('discovery_receipt_invalid')
            key = _candidate_key(candidate)
            if key in identities:
                raise DiscoveryIncomplete('discovery_identity_ambiguous')
            identities[key] = candidate['id']
        new_candidates, members = [], []
        for position, c in enumerate(batch.candidates):
            values = dict(description=c.description, type=c.type, evidence=c.evidence,
                evidence_start=c.evidence_start, evidence_end=c.evidence_end)
            candidate_id = identities.get(_candidate_key(values))
            if candidate_id is None:
                candidate_id = uuid5(run_id, f'{c.evidence_start}:{c.evidence_end}')
                new_candidates.append(dict(values, id=candidate_id, run_id=run_id, reason=c.reason,
                    status='pending', created_at=_utcnow()))
            members.append(dict(run_id=run_id, candidate_id=candidate_id, position=position, reason=c.reason))
        if new_candidates:
            await db.execute(insert(Candidate).values(new_candidates))
        await db.execute(insert(RunItem).values(members))
    return len(batch.candidates)
