"""LLM call log — captures every prompt invocation for observability."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class LLMCallLog(Base):
    __tablename__ = "llm_call_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_id = Column(
        UUID(as_uuid=True),
        ForeignKey("prompt_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_type = Column(String(50), nullable=False, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    messages_json = Column(JSON, nullable=False)
    rag_hits_json = Column(JSON, nullable=True)
    response_text = Column(Text, default="")
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    model = Column(String(200), default="")
    endpoint_id = Column(
        UUID(as_uuid=True),
        ForeignKey("llm_endpoints.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(20), default="ok")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, index=True)
