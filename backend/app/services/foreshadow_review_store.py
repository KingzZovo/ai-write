"""Persist unconfirmed reviews in a separate, short source-checked transaction."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.project import Chapter, Volume, Foreshadow, ForeshadowReview, _utcnow
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, foreshadow_source_hash
from app.services.foreshadow_review import ScopedResolutionReview


async def save_resolution_review(review: ScopedResolutionReview) -> int:
    """Own the write transaction; do not commit a hook's borrowed session."""
    if not review.suggestions:
        return 0
    async with async_session_factory() as db:
        async with db.begin():
            return await persist_resolution_review(db, review)


async def persist_resolution_review(db: AsyncSession, review: ScopedResolutionReview, *, strict_source: bool = False) -> int:
    """Caller owns transaction. Lock chapter then foreshadows in ID order.

    Reports are immutable snapshots. Repeated inference must not reset a
    previously dismissed/accepted review to pending or replace its evidence.
    """
    if not review.suggestions:
        return 0
    project_id, volume_id, chapter_id = UUID(review.project_id), UUID(review.volume_id), UUID(review.chapter_id)
    row = (await db.execute(
        select(Chapter.global_idx, Chapter.content_text, Chapter.updated_at, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id, Chapter.id == chapter_id,
               Chapter.volume_id == volume_id, Chapter.chapter_idx == review.chapter_idx)
        .with_for_update(of=(Chapter, Volume)).execution_options(autoflush=False)
    )).one_or_none()
    if row is None:
        if strict_source: raise ValueError('review_source_changed')
        return 0
    global_idx, text, updated_at, status = row
    if (global_idx != review.global_idx or updated_at != review.source_updated_at
            or status != review.source_status or not isinstance(text, str)
            or hashlib.sha256(text.encode('utf-8')).hexdigest() != review.source_sha256):
        if strict_source: raise ValueError('review_source_changed')
        return 0

    ids = sorted({UUID(s.foreshadow_id) for s in review.suggestions})
    current = (await db.execute(
        select(Foreshadow.id, *(getattr(Foreshadow, name) for name in REVIEW_SOURCE_FIELDS))
        .where(Foreshadow.project_id == project_id, Foreshadow.id.in_(ids))
        .order_by(Foreshadow.id).with_for_update().execution_options(autoflush=False)
    )).mappings().all()
    sources = {str(row['id']): row for row in current}
    inserted = 0
    for suggestion in review.suggestions:
        source = sources.get(suggestion.foreshadow_id)
        if source is None or foreshadow_source_hash(source) != suggestion.foreshadow_sha256:
            if strict_source: raise ValueError('review_source_changed')
            continue
        if (suggestion.source_sha256 != review.source_sha256 or suggestion.chapter_idx != global_idx
                or type(suggestion.evidence_start) is not int or type(suggestion.evidence_end) is not int
                or suggestion.evidence_start < 0 or suggestion.evidence_end <= suggestion.evidence_start
                or suggestion.evidence_end > len(text)
                or text[suggestion.evidence_start:suggestion.evidence_end] != suggestion.evidence
                or not suggestion.evidence.strip() or not suggestion.reason.strip()):
            raise ValueError('invalid_foreshadow_review_evidence')
        snapshot = {
            'chapter_id': review.chapter_id, 'volume_id': review.volume_id,
            'chapter_idx': review.chapter_idx, 'global_idx': review.global_idx,
            'source_sha256': review.source_sha256,
            'source_updated_at': updated_at.isoformat() if updated_at else None,
            'source_status': status, 'foreshadow_sha256': suggestion.foreshadow_sha256,
            'foreshadow': {name: source[name] for name in REVIEW_SOURCE_FIELDS},
        }
        snapshot_hash = hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
        ).encode('utf-8')).hexdigest()
        result = await db.execute(insert(ForeshadowReview).values(
            id=uuid4(), project_id=project_id, chapter_id=chapter_id,
            foreshadow_id=UUID(suggestion.foreshadow_id), snapshot_sha256=snapshot_hash,
            source_snapshot=snapshot, evidence=suggestion.evidence,
            evidence_start=suggestion.evidence_start, evidence_end=suggestion.evidence_end,
            reason=suggestion.reason, status='pending', created_at=_utcnow(),
        ).on_conflict_do_nothing(index_elements=['chapter_id', 'foreshadow_id', 'snapshot_sha256'])
         .returning(ForeshadowReview.id))
        if result.scalar_one_or_none() is not None:
            inserted += 1
    return inserted
