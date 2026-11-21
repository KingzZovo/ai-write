"""Read review snapshots with current-source checks, without changing state."""
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from app.models.project import Chapter, Volume, Foreshadow, ForeshadowReview, ForeshadowConfirmationJob
from app.services.foreshadow_provenance import REVIEW_SOURCE_FIELDS, REVIEWABLE_STATUSES, foreshadow_source_hash


def review_source_state(review, chapter, foreshadow):
    """PG matches cannot establish that a Neo4j-owned record is synchronized."""
    snapshot = review['source_snapshot']
    if not isinstance(snapshot, dict):
        return 'unknown'
    try:
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if digest != review['snapshot_sha256']:
            return 'unknown'
        if chapter is None or foreshadow is None:
            return 'unavailable'
        text = chapter['content_text']
        if not isinstance(text, str):
            return 'changed'
        expected = {
            'chapter_id': str(review['chapter_id']), 'volume_id': str(chapter['volume_id']),
            'chapter_idx': chapter['chapter_idx'], 'global_idx': chapter['global_idx'],
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'source_updated_at': chapter['updated_at'].isoformat() if chapter['updated_at'] else None,
            'source_status': chapter['status'], 'foreshadow_sha256': foreshadow_source_hash(foreshadow),
            'foreshadow': {key: foreshadow[key] for key in REVIEW_SOURCE_FIELDS},
        }
        comparison = snapshot
        accepted = review.get('status') == 'accepted'
        if accepted:
            # Preserve the original evidence snapshot. Compare current records
            # with the exact PG mutation performed by author acceptance.
            before = snapshot['foreshadow']
            if (before['source'] not in ('lifecycle', 'neo4j') or before['status'] not in REVIEWABLE_STATUSES
                    or foreshadow_source_hash(before) != snapshot['foreshadow_sha256']):
                return 'unknown'
            after = dict(before, status='resolved', resolved_chapter=snapshot['global_idx'])
            comparison = dict(snapshot, foreshadow=after, foreshadow_sha256=foreshadow_source_hash(after))
        if comparison != expected:
            return 'changed'
        start, end = review['evidence_start'], review['evidence_end']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or text[start:end] != review['evidence']):
            return 'unknown'
        if accepted:
            return 'accepted_current' if foreshadow['source'] == 'lifecycle' else 'projection_current'
        return 'current' if foreshadow['source'] == 'lifecycle' else 'projection_current'
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'unknown'


async def list_resolution_reviews(db, project_id: UUID, status: str | None, limit: int, offset: int):
    query = select(*ForeshadowReview.__table__.columns).where(ForeshadowReview.project_id == project_id)
    if status is not None:
        query = query.where(ForeshadowReview.status == status)
    rows = list((await db.execute(query.order_by(ForeshadowReview.created_at.desc(), ForeshadowReview.id.desc())
        .offset(offset).limit(limit + 1).execution_options(autoflush=False))).mappings().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return {'reviews': [], 'has_more': has_more}
    chapters = (await db.execute(select(Chapter.id, Chapter.volume_id, Chapter.chapter_idx,
        Chapter.global_idx, Chapter.content_text, Chapter.updated_at, Chapter.status)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == project_id, Chapter.id.in_({r['chapter_id'] for r in rows}))
        .execution_options(autoflush=False))).mappings().all()
    foreshadows = (await db.execute(select(Foreshadow.id, *(getattr(Foreshadow, k) for k in REVIEW_SOURCE_FIELDS))
        .where(Foreshadow.project_id == project_id, Foreshadow.id.in_({r['foreshadow_id'] for r in rows}))
        .execution_options(autoflush=False))).mappings().all()
    chapter_map, fs_map = {r['id']: r for r in chapters}, {r['id']: r for r in foreshadows}
    confirming = {row['id'] for row in rows if row['status'] == 'confirming'}
    confirmations = dict((await db.execute(select(ForeshadowConfirmationJob.id, ForeshadowConfirmationJob.status)
        .where(ForeshadowConfirmationJob.project_id == project_id, ForeshadowConfirmationJob.id.in_(confirming))
        .execution_options(autoflush=False))).all()) if confirming else {}
    reviews = []
    for row in rows:
        state = review_source_state(row, chapter_map.get(row['chapter_id']), fs_map.get(row['foreshadow_id']))
        # Unverified metadata may have an invalid nested shape. Do not let it
        # impersonate a trusted description or break the review renderer.
        reviews.append(dict(row, source_state=state, confirmation_status=confirmations.get(row['id']),
            source_snapshot=row['source_snapshot'] if state != 'unknown' else {}))
    return {'reviews': reviews, 'has_more': has_more}
