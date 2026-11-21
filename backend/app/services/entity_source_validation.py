"""Reuse exact body/quote proofs while reading current entity records each time.

This verifies saved source identity and literal evidence, not the truth of an
extracted claim. No source bodies or mutable graph/PG entity rows are cached.
"""
from uuid import UUID

from app.services import entity_sources
from app.services.source_proof_cache import EvidenceProofCache


entity_evidence_proofs = EvidenceProofCache()


async def filter_source_rows(db, project_id, rows, *, before_global_idx):
    pid = UUID(str(project_id))
    candidates, ids = [], set()
    for row in rows:
        raw_id, index = str(row.get('source_chapter_id')), row.get('global_idx')
        try:
            identity = UUID(raw_id)
        except (ValueError, TypeError):
            continue
        if (str(identity) != raw_id or type(index) is not int or not 0 < index < before_global_idx
                or not isinstance(row.get('source_hash'), str)):
            continue
        excerpt = row.get('excerpt')
        if 'excerpt' in row and (not isinstance(excerpt, str) or not excerpt.strip()):
            continue
        candidates.append((row, raw_id, excerpt))
        ids.add(identity)
    if not candidates:
        return []

    # The optional bound excludes current/future bodies before legacy hashes
    # fall back to full reads. Headers alone are never treated as body proofs.
    sources = await entity_sources.resolve_entity_source_headers(db, pid, ids, before_global_idx=before_global_idx)
    prepared, proofs, approved = [], {}, set()
    for row, cid, excerpt in candidates:
        source = sources.get(cid)
        if (source is None or source.source_hash != row['source_hash']
                or source.global_idx != row['global_idx']):
            continue
        key = entity_sources.digest(dict(version=1, source_hash=source.source_hash, excerpt=excerpt))
        prepared.append((row, key))
        proofs[key] = (cid, excerpt)
    missing = {}
    for key, value in proofs.items():
        if entity_evidence_proofs.contains(key):
            approved.add(key)
        else:
            missing[key] = value

    needed = {cid for cid, _ in missing.values()}
    bodies = {cid: sources[cid] for cid in needed if isinstance(sources[cid], entity_sources.EntitySource)}
    bodies.update(await entity_sources.resolve_entity_sources(db, pid, needed - bodies.keys()))
    for key, (cid, excerpt) in missing.items():
        body = bodies.get(cid)
        if body is None or body.receipt != sources[cid].receipt:
            continue  # An edit raced the full-body proof, or the source is blank.
        if excerpt is not None and excerpt not in body.content:
            continue
        entity_evidence_proofs.remember(key)
        approved.add(key)
    return [row for row, key in prepared if key in approved]
