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

ALL_PHASES = (
    PHASE_PLANNING,
    PHASE_DRAFTING,
    PHASE_CRITIC,
    PHASE_REWRITE,
    PHASE_FINALIZE,
    PHASE_COMPACT,
    PHASE_DONE,
    PHASE_FAILED,
)

# Run status (independent of phase):
#   running  — actively processing the current phase
#   paused   — checkpointed; can be resumed
#   done     — run finished successfully
#   failed   — run hit terminal error after retries
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


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
    # Per-phase checkpoint: {planning:{pack:{...}}, drafting:{text:""}, critic:{report:{...}}, finalize:{final_text:""}}
    checkpoint_data = Column(JSON, default=dict, nullable=False)
    last_error = Column(Text, default="", nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
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
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    run = relationship("GenerationRun", back_populates="critic_reports")
