"""Durable source-bound entity payloads and projection attempts."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, JSON, DateTime, ForeignKey, UniqueConstraint, BigInteger, Sequence, Index, func, Integer
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base

entity_projection_tokens = Sequence('entity_projection_tokens', metadata=Base.metadata)

class ChapterEntityExtraction(Base):
    __tablename__ = 'chapter_entity_extractions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chapter_id = Column(UUID(as_uuid=True), ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False)
    source_hash = Column(String(64), nullable=False)
    source_json = Column(JSON, nullable=False)
    payload_json = Column(JSON, nullable=True)
    payload_hash = Column(String(64), nullable=True)
    payload_stage = Column(String(24), nullable=False, default='complete', server_default='complete')
    scope_json = Column(JSON(none_as_null=True), nullable=True)
    delivery_id = Column(String(100), nullable=True)
    projection_token = Column(BigInteger, entity_projection_tokens, server_default=entity_projection_tokens.next_value(), nullable=False)
    projection_version = Column(Integer, nullable=False, server_default='1')
    status = Column(String(24), nullable=False, default='pending')
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (
        UniqueConstraint('chapter_id', 'source_hash', name='uq_chapter_entity_source'),
        Index('ix_entity_extraction_retry', 'status', 'updated_at'),
    )


entity_source_outbox_revisions = Sequence('entity_source_outbox_revisions', metadata=Base.metadata)


class EntitySourceOutbox(Base):
    """Latest completed-source change, captured by migration-installed PG triggers.

    This is a delivery outbox, not a deletion/invalidation tombstone ledger.
    """
    __tablename__ = 'entity_source_outbox'
    chapter_id = Column(UUID(as_uuid=True), ForeignKey('chapters.id', ondelete='CASCADE'), primary_key=True)
    revision = Column(BigInteger, entity_source_outbox_revisions,
                      server_default=entity_source_outbox_revisions.next_value(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index('ix_entity_source_outbox_pending', 'updated_at', 'chapter_id'),)


entity_invalidation_revisions = Sequence('entity_invalidation_revisions', metadata=Base.metadata)


class EntityInvalidation(Base):
    """Cleanup work deliberately has no FK to deleted author records."""
    __tablename__ = 'entity_invalidations'
    project_id = Column(UUID(as_uuid=True), primary_key=True)
    chapter_id = Column(UUID(as_uuid=True), primary_key=True)
    revision = Column(BigInteger, entity_invalidation_revisions,
                      server_default=entity_invalidation_revisions.next_value(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_error = Column(String(128))
    __table_args__ = (Index('ix_entity_invalidations_pending', 'updated_at'),)


class OutlineSettingsExtraction(Base):
    """Durable validated settings payload; recovery never calls an LLM."""
    __tablename__ = 'outline_settings_extractions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    outline_id = Column(UUID(as_uuid=True), ForeignKey('outlines.id', ondelete='CASCADE'), nullable=False)
    source_hash = Column(String(64), nullable=False)
    payload_json = Column(JSON, nullable=False)
    status = Column(String(24), nullable=False, default='pending')
    attempts = Column(Integer, nullable=False, default=0)
    counts_json = Column(JSON, nullable=True)
    last_error = Column(String(128), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        UniqueConstraint('outline_id', 'source_hash', name='uq_outline_settings_source'),
        Index('ix_outline_settings_retry', 'status', 'next_attempt_at'),
    )


class AppliedCharacterDeletion(Base):
    """PG commit journal: an old graph receipt cannot delete a recreated row."""
    __tablename__ = 'applied_character_deletions'
    token = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), primary_key=True)
    applied_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
