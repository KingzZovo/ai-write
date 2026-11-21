"""Bounded memoization of complete, exact-source cognition evidence checks.

Only opaque digests are cached, never prose or mutable model/author payloads.
Current metadata and actual payloads must still be read and checked each time.
The database-maintained content hash is required to avoid repeated body reads.
"""
from copy import deepcopy
from dataclasses import dataclass

from app.services.cognition_reader import effective_change_payload, effective_changes, record_version
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import EntitySource, digest, resolve_entity_sources
from app.services.source_proof_cache import EvidenceProofCache


evidence_proofs = EvidenceProofCache()


class CognitionLedger(dict):
    """Ordinary history mapping with a witness for every source, including []."""
    snapshot_hash: str | None = None
    snapshot: dict | None = None


@dataclass(frozen=True)
class CognitionPayload:
    changes: list[dict]
    version: str
    proof_key: str


def read_payload(record, source, revision=None):
    """Revalidate even on cache hits; changed raw data cannot reuse a proof."""
    changes = effective_change_payload(record, source, revision)
    version = record_version(record, source)
    key = digest(dict(version=1, source=source.receipt, record_version=version,
        changes=changes, author_payload_hash=revision.payload_hash if revision else None))
    return CognitionPayload(deepcopy(changes), version, key)


async def validate_evidence_batch(db, project_id, sources, records, revisions):
    """Read complete bodies only for uncached exact payloads, in bounded batches."""
    payloads = {}
    missing = []
    for cid, source in sources.items():
        record = records.get(cid)
        revision = revisions.get(record.author_revision_id) if record else None
        payload = payloads[cid] = read_payload(record, source, revision)
        if not evidence_proofs.contains(payload.proof_key):
            missing.append(cid)
    bodies = {cid: sources[cid] for cid in missing if isinstance(sources[cid], EntitySource)}
    bodies.update(await resolve_entity_sources(db, project_id, [cid for cid in missing if cid not in bodies],
                                               allow_review_drafts=True))
    for cid in missing:
        source = bodies.get(cid)
        if source is None or source.receipt != sources[cid].receipt:
            raise RequiredContextUnavailable('character_cognition_source_unavailable')
        record = records[cid]
        revision = revisions.get(record.author_revision_id)
        if read_payload(record, source, revision).proof_key != payloads[cid].proof_key:
            raise RequiredContextUnavailable('character_cognition_decision_changed')
        effective_changes(record, source, revision)
        evidence_proofs.remember(payloads[cid].proof_key)
    return payloads
