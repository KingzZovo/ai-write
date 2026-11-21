"""Original arc requests and durable planning checkpoints outlive their volumes."""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class ArcPlanningRequest(Base):
    __tablename__ = "arc_planning_requests"

    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    request_json = Column(JSON, nullable=False)
    request_hash = Column(String(64), nullable=False)
    source_json = Column(JSON, nullable=False)
    source_hash = Column(String(64), nullable=False)
    journal_json = Column(JSON, nullable=False)
    journal_hash = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    phase = Column(String(40), nullable=False, default="meta")
    target_chapters = Column(Integer, nullable=False)
    target_volume_idx = Column(Integer, nullable=False)
    accepted_chapters = Column(Integer, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=1)
    execution_id = Column(UUID(as_uuid=True))
    execution_attempts = Column(Integer, nullable=False, default=0)
    execution_history_json = Column(JSON, nullable=False, default=list)
    error_code = Column(String(64), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    result_json = Column(JSON(none_as_null=True))
    result_hash = Column(String(64))
    heartbeat_at = Column(DateTime(timezone=True))
    dispatched_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint("target_chapters BETWEEN 4 AND 40", name="ck_arc_planning_target"),
        CheckConstraint("target_volume_idx > 0", name="ck_arc_planning_volume_index"),
        CheckConstraint("execution_attempts BETWEEN 0 AND 3", name="ck_arc_planning_execution_budget"),
        CheckConstraint("revision > 0", name="ck_arc_planning_revision"),
        CheckConstraint("accepted_chapters BETWEEN 0 AND target_chapters", name="ck_arc_planning_progress"),
        CheckConstraint("status IN ('pending', 'running', 'needs_review', 'completed', 'cancelled')", name="ck_arc_planning_status"),
        Index("ix_arc_planning_history", "project_id", "created_at", "id"),
        Index("ix_arc_planning_dispatch", "status", "dispatched_at", "heartbeat_at"),
        Index("uq_arc_planning_active", "project_id", unique=True,
              postgresql_where=text("status IN ('pending', 'running')"),
              sqlite_where=text("status IN ('pending', 'running')")),
    )
