"""Generation task model — tracks async outline/chapter generation jobs."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class GenerationTask(Base):
    """A background generation task (outline or chapter)."""

    __tablename__ = "generation_tasks"
    __table_args__ = (Index('ix_generation_tasks_recovery', 'status', 'dispatched_at', 'created_at', 'id'),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), nullable=False)
    task_type = Column(String(30), nullable=False)  # outline_book, outline_volume, outline_chapter, chapter
    status = Column(String(20), default="pending")  # pending, running, completed, failed
    progress_text = Column(Text, default="")  # accumulated generated text so far
    result_text = Column(Text, default="")  # original version (raw)
    polished_text = Column(Text, default="")  # anti-AI polished version
    error_message = Column(Text, nullable=True)
    params_json = Column(JSON, default=dict)  # user_input, style_id, chapter_id, etc.
    char_count = Column(Integer, default=0)
    # Independent execution provenance. Version 0 is historical/unverified;
    # batch and whole-book children retain their own ownership contracts.
    execution_version = Column(Integer, nullable=False, default=0, server_default='0')
    execution_id = Column(UUID(as_uuid=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    execution_params_json = Column(JSON, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default='0')
    attempts_json = Column(JSON, nullable=False, default=list, server_default='[]')
    saved_manuscript_json = Column(JSON, nullable=True)
    execution_source_json = Column(JSON, nullable=True)
    progress_kind = Column(String(20), nullable=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    recovery_history_json = Column(JSON, nullable=False, default=list, server_default='[]')
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
