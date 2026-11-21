"""LLM call log — captures every prompt invocation for observability."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class LLMCallLog(Base):
    """NOTE (a1001916): in Postgres this table is RANGE-partitioned by
    ``created_at`` (monthly), and the DB primary key is the composite
    ``(id, created_at)`` — partitioned unique constraints must include
    the partition key. The mapper keeps a single-column PK (``id`` is a
    uuid4, unique in practice) so ``db.get(LLMCallLog, id)`` and the
    identity map keep working unchanged. Do not add code that relies on
    a DB-level unique constraint on ``id`` alone.

    Retention: monthly partitions older than
    ``settings.LLM_LOG_RETENTION_MONTHS`` are dropped by
    ``tasks.maintain_llm_log_partitions``.
    """

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
    # a1001960: prompt-cache hits, a *subset* of ``input_tokens`` (the
    # provider bills cached input at a lower rate). Never added on top of
    # ``input_tokens`` when computing totals. NOT NULL with a server default
    # so pre-existing (partitioned) rows read as 0 instead of NULL.
    cached_input_tokens = Column(Integer, nullable=False, server_default="0", default=0)
    # a1001961: True ⇒ ``input_tokens`` / ``output_tokens`` are a
    # character-count *estimate*, not what the provider billed. Some relays
    # never return usage (even with ``stream_options.include_usage``), so
    # streaming calls used to persist 0/0 for every attempt. Estimator fills
    # them and flags the row; real provider numbers are never overwritten and
    # never flagged. NOT NULL with a server default so pre-existing rows read
    # as false instead of NULL.
    usage_estimated = Column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
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
    # v1.5.0 B1 - tier-aware fallback fields
    tier_used = Column(String(20), nullable=True, index=True)
    fallback_reason = Column(String(200), nullable=True)
    attempt_index = Column(Integer, default=0, nullable=False)

    # NOT NULL since a1001916 (partition key). index=True materialized as
    # ix_llm_call_logs_created_at on the partitioned parent by the same
    # migration (it had never been created on the old flat table).
    created_at = Column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
