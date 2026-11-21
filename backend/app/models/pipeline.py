"""Pipeline ORM models for production pipeline state machine."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731

# Pipeline states
PIPELINE_STATES = ["planning", "queued", "generating", "pausing", "reviewing", "polishing", "completed", "paused", "failed", "needs_review"]


class PipelineRun(Base):
    """A production pipeline run for generating a full book or volume."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_dispatch", "state", "dispatched_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    volume_id = Column(UUID(as_uuid=True), nullable=True)  # None = entire book

    state = Column(String(20), default="planning")
    current_chapter_idx = Column(Integer, default=0)
    total_chapters = Column(Integer, default=0)
    completed_chapters = Column(Integer, default=0)

    # Review tracking
    review_round = Column(Integer, default=0)
    max_review_rounds = Column(Integer, default=3)

    # Snapshot for rollback
    snapshot_json = Column(JSON, nullable=True)
    options_json = Column(JSON, default=dict)

    # Durable dispatch + worker fencing. Broker delivery is a notification;
    # command_id identifies the persisted command that a worker may claim.
    command_id = Column(UUID(as_uuid=True), nullable=True)
    worker_id = Column(UUID(as_uuid=True), nullable=True)
    queued_at = Column(DateTime(timezone=True), nullable=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)

    # Error info
    error_message = Column(Text, nullable=True)
    last_error_chapter = Column(Integer, nullable=True)

    # Timing
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PipelineChapterStatus(Base):
    """Per-chapter status within a pipeline run."""

    __tablename__ = "pipeline_chapter_statuses"
    __table_args__ = (Index("ix_pipeline_chapter_statuses_run", "pipeline_id"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False)
    chapter_id = Column(UUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False)
    chapter_idx = Column(Integer, nullable=False)

    state = Column(String(20), default="pending")  # pending, generating, reviewing, polishing, completed, failed, needs_review
    review_round = Column(Integer, default=0)
    word_count = Column(Integer, default=0)
    quality_score = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    generation_task_id = Column(UUID(as_uuid=True), nullable=True)
    attempt_count = Column(Integer, default=0, nullable=False, server_default="0")
    saved_hash = Column(String(64), nullable=True)
    saved_updated_at = Column(DateTime(timezone=True), nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
