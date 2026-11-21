"""Source-bound chapter cognition deltas and durable extraction work."""
import uuid
from sqlalchemy import Column, String, JSON, DateTime, ForeignKey, UniqueConstraint, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base


def _initial_cycle(context):
    return context.get_current_parameters()['id']


class ChapterCognition(Base):
    __tablename__ = 'chapter_cognition_extractions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chapter_id = Column(UUID(as_uuid=True), ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False)
    source_hash = Column(String(64), nullable=False)
    source_json = Column(JSON, nullable=False)
    changes_json = Column(JSON, nullable=True)
    payload_hash = Column(String(64), nullable=True)
    source_mode = Column(String(16), nullable=False, default='auto', server_default='auto')
    author_revision_id = Column(UUID(as_uuid=True), nullable=True)
    attempt_token = Column(UUID(as_uuid=True), nullable=True)
    cycle_id = Column(UUID(as_uuid=True), nullable=False, default=_initial_cycle)
    delivery_id = Column(String(100), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default='0')
    status = Column(String(24), nullable=False, default='pending')
    last_error = Column(String(128), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint('chapter_id', 'source_hash', name='uq_chapter_cognition_source'),
        Index('ix_chapter_cognition_retry', 'status', 'updated_at'),
        Index('ix_chapter_cognition_project', 'project_id'),
    )


class CognitionRevision(Base):
    """Append-only author decisions survive chapter deletion, never overwrite model output."""
    __tablename__ = 'cognition_revisions'
    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chapter_id = Column(UUID(as_uuid=True), nullable=False)
    record_id = Column(UUID(as_uuid=True), nullable=False)
    previous_id = Column(UUID(as_uuid=True), nullable=True)
    source_hash = Column(String(64), nullable=False)
    source_json = Column(JSON, nullable=False)
    base_version = Column(String(64), nullable=False)
    mode = Column(String(16), nullable=False)
    changes_json = Column(JSON, nullable=True)
    model_snapshot_json = Column(JSON, nullable=False)
    actor = Column(String(256), nullable=True)
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (Index('ix_cognition_revision_chapter', 'project_id', 'chapter_id', 'created_at', 'id'),)


class CognitionRequest(Base):
    """Immutable acknowledgment; chapter deletion must not replay a request."""
    __tablename__ = 'cognition_requests'
    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    request_hash = Column(String(64), nullable=False)
    result_json = Column(JSON, nullable=True)  # Filled in the same transaction.
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CognitionBackfill(Base):
    """Frozen chapter IDs and durable cursor; scheduled does not mean extracted."""
    __tablename__ = 'cognition_backfills'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chapter_ids = Column(JSON, nullable=False)
    chapter_count = Column(Integer, nullable=False)
    cursor = Column(Integer, nullable=False, default=0, server_default='0')
    added_count = Column(Integer, nullable=False, default=0, server_default='0')
    existing_count = Column(Integer, nullable=False, default=0, server_default='0')
    attention_count = Column(Integer, nullable=False, default=0, server_default='0')
    unavailable_count = Column(Integer, nullable=False, default=0, server_default='0')
    status = Column(String(24), nullable=False, default='pending')
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        Index('uq_cognition_backfill_active', 'project_id', unique=True,
              postgresql_where=text("status = 'pending'"), sqlite_where=text("status = 'pending'")),
        Index('ix_cognition_backfill_recovery', 'status', 'updated_at'),
    )
