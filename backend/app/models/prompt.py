"""Prompt asset ORM model for the Prompt Registry."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class PromptAsset(Base):
    """A versioned prompt template stored in the registry."""

    __tablename__ = "prompt_assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type = Column(String(50), nullable=False)  # generation, outline, evaluation, extraction, etc.
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    mode = Column(String(20), default="text")  # text | structured (JSON output)
    system_prompt = Column(Text, nullable=False)
    user_template = Column(Text, default="")  # template with {{variables}}
    output_schema = Column(JSON, nullable=True)  # expected JSON schema for structured mode
    context_policy = Column(String(50), default="default")  # how context is injected
    version = Column(Integer, default=1)
    is_active = Column(Integer, default=1)
    success_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    avg_score = Column(Integer, default=0)  # 0-100
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
