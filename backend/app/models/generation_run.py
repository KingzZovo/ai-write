"""v0.7 — Generation state machine + Critic tables.

A GenerationRun tracks one pipelined chapter generation through phases
(planning -> drafting -> critic -> [rewrite] -> finalize -> compact?).
Every phase persists a checkpoint so the run can resume after a crash.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base


# Phase values used as the state machine nodes.
PHASE_PLANNING = "planning"
PHASE_DRAFTING = "drafting"
PHASE_CRITIC = "critic"
PHASE_REWRITE = "rewrite"
PHASE_FINALIZE = "finalize"
PHASE_COMPACT = "compact"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_AWAITING_REVIEW = "awaiting_review"

ALL_PHASES = (
    PHASE_PLANNING,
    PHASE_DRAFTING,
    PHASE_CRITIC,
    PHASE_REWRITE,
    PHASE_FINALIZE,
    PHASE_COMPACT,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_AWAITING_REVIEW,
)

# Run status (independent of phase):
#   running  — actively processing the current phase
#   paused   — checkpointed; can be resumed
#   done     — automation finished, author review is still required
#   failed   — run hit terminal error after retries
#   needs_review — incomplete review/delivery or unresolved manuscript issues
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NEEDS_REVIEW = "needs_review"


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Current state
    phase = Column(String(32), default=PHASE_PLANNING, nullable=False)
    status = Column(String(16), default=STATUS_RUNNING, nullable=False)
    # Per-phase delivery data plus the versioned attempt_journal on new runs.
    # Missing legacy history must never be synthesized during resume.
    checkpoint_data = Column(JSON, default=dict, nullable=False)
    last_error = Column(Text, default="", nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    # New protocol: consumed logical rewrite attempts, including interruptions.
    # Reserved before the model with attempt_journal in the same transaction.
    rewrite_count = Column(Integer, default=0, nullable=False)
    max_rewrite_count = Column(Integer, default=2, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    critic_reports = relationship(
        "CriticReport",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class GenerationRunSubmission(Base):
    """Immutable request identity, retained after deleting its run/chapter.

    The client request UUID is also the original GenerationRun UUID. There
    is deliberately no foreign key to that run or chapter: their deletion
    must not make an already accepted request available for reuse.
    """
    __tablename__ = "generation_run_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    chapter_id = Column(UUID(as_uuid=True), nullable=False)
    version = Column(Integer, nullable=False)
    request_hash = Column(String(64), nullable=False)
    max_rewrite_count = Column(Integer, nullable=False)
    source_json = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )


class CriticReport(Base):
    __tablename__ = "critic_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    round = Column(Integer, default=1, nullable=False)
    issues_json = Column(JSON, default=list, nullable=False)
    # Delivery stages and exact-draft digest; old reports remain unknown.
    review_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    run = relationship("GenerationRun", back_populates="critic_reports")
