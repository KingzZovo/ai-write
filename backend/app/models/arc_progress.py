"""Append-only author arc decisions; chapter and volume deletion keep receipts."""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class ArcProgressWrite(Base):
    __tablename__ = "arc_progress_writes"

    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    volume_id = Column(UUID(as_uuid=True), nullable=False)
    sequence = Column(Integer, nullable=False)
    action = Column(String(32), nullable=False)
    chapter_id = Column(UUID(as_uuid=True))
    manuscript_hash = Column(String(64))
    request_hash = Column(String(64), nullable=False)
    before_json = Column(JSON, nullable=False)
    manuscript_json = Column(JSON, nullable=False)
    result_json = Column(JSON, nullable=False)
    result_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_arc_progress_sequence_positive"),
        UniqueConstraint("project_id", "volume_id", "sequence", name="uq_arc_progress_sequence"),
        Index("ix_arc_progress_history", "project_id", "volume_id", "sequence"),
    )
