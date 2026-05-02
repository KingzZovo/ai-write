"""v1.5.0 C4 — cascade_tasks ORM model.

When a `chapter_evaluations` row reports `rounds_exhausted=true` AND
`overall < threshold` AND `issues_json` carries severity>=high cross_chapter /
foreshadow / consistency issues, the failing chapter is fed to the C4 planner
which derives the affected upstream entities (chapter / outline / character /
world_rule) and enqueues one Celery `cascade_task` per (entity_type, entity_id,
severity) tuple.

Workflow:
    pending -> running -> (done | failed | skipped)

Idempotency: UNIQUE (source_chapter_id, target_entity_type,
target_entity_id, severity).  Re-running the planner on the same evaluation
row must not duplicate enqueue.

Serialization: per-project FIFO. Limited via `(project_id, status)` index +
planner-side advisory lock / `SELECT ... FOR UPDATE`.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.session import Base
from app.models.project import _utcnow


# Allowed enum values (kept as plain tuples — `String` + CHECK constraint
# avoids the migration-vs-Base.metadata friction PG enum types create).
TARGET_ENTITY_TYPES = ("chapter", "outline", "character", "world_rule")
SEVERITIES = ("high", "critical")
STATUSES = ("pending", "running", "done", "failed", "skipped")


class CascadeTask(Base):
    """One enqueued upstream-fix task derived from a failing chapter evaluation."""

    __tablename__ = "cascade_tasks"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Per-project serial limiter.
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Failing chapter that triggered this cascade entry.
    source_chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Which evaluation row produced the issues_json we parsed.
    source_evaluation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapter_evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )

    target_entity_type = Column(String(20), nullable=False)
    target_entity_id = Column(UUID(as_uuid=True), nullable=False)

    severity = Column(String(20), nullable=False)
    issue_summary = Column(Text, nullable=True)

    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        default="pending",
    )

    # Optional self-FK so a planner-driven follow-up can record its parent.
    parent_task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("cascade_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )

    attempt_count = Column(Integer, nullable=False, server_default="0", default=0)
    error_message = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=_utcnow,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships (kept light — we don't want eager loading by default).
    parent = relationship(
        "CascadeTask",
        remote_side="CascadeTask.id",
        backref="children",
    )

    __table_args__ = (
        CheckConstraint(
            "target_entity_type IN ('chapter','outline','character','world_rule')",
            name="ck_cascade_tasks_target_entity_type",
        ),
        CheckConstraint(
            "severity IN ('high','critical')",
            name="ck_cascade_tasks_severity",
        ),
        CheckConstraint(
            "status IN ('pending','running','done','failed','skipped')",
            name="ck_cascade_tasks_status",
        ),
        UniqueConstraint(
            "source_chapter_id",
            "target_entity_type",
            "target_entity_id",
            "severity",
            name="uq_cascade_tasks_idem",
        ),
        Index("ix_cascade_tasks_project_status", "project_id", "status"),
        Index("ix_cascade_tasks_source_chapter", "source_chapter_id"),
        Index(
            "ix_cascade_tasks_target_entity",
            "target_entity_type",
            "target_entity_id",
        ),
    )
