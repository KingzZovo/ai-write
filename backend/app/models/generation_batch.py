"""Durable batch requests and per-chapter observations of canonical tasks."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class GenerationBatch(Base):
    __tablename__ = 'generation_batches'

    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    request_hash = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    total_chapters = Column(Integer, nullable=False)
    worker_id = Column(UUID(as_uuid=True), nullable=True)
    # Zero identifies executions from before the recovery protocol. Never
    # infer a saved-body receipt or a live lease for those historical rows.
    execution_version = Column(Integer, nullable=False, default=0, server_default='0')
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    recovery_count = Column(Integer, nullable=False, default=0, server_default='0')
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index('ix_generation_batches_project', 'project_id', 'created_at', 'id'),
        Index('ix_generation_batches_recovery', 'status', 'execution_version', 'heartbeat_at', 'dispatched_at'),
    )


class GenerationBatchItem(Base):
    __tablename__ = 'generation_batch_items'

    task_id = Column(UUID(as_uuid=True), primary_key=True)
    batch_id = Column(UUID(as_uuid=True), ForeignKey('generation_batches.id', ondelete='CASCADE'), nullable=False)
    # Preserve the submitted identity and result if a chapter/task is removed.
    chapter_id = Column(UUID(as_uuid=True), nullable=False)
    volume_id = Column(UUID(as_uuid=True), nullable=False)
    volume_idx = Column(Integer, nullable=False)
    chapter_idx = Column(Integer, nullable=False)
    position = Column(Integer, nullable=False)
    outline_json = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    word_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default='0')
    attempts_json = Column(JSON, nullable=False, default=list, server_default='[]')
    saved_receipt_json = Column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint('batch_id', 'position', name='uq_generation_batch_position'),)
