"""Generation task model — tracks async outline/chapter generation jobs."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class GenerationTask(Base):
    """A background generation task (outline or chapter)."""

    __tablename__ = "generation_tasks"

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
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
