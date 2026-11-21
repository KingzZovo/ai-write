"""v0.8 — Writing-engine asset tables.

Five resources that let us lift network-novel craft knowledge out of implicit
system prompts into editable, queryable, audit-able assets:

- WritingRule     : paragraph-level rules (pacing / dialogue / hook / description).
- BeatPattern     : reusable plot beats (opening / volume_end / climax / ...).
- AntiAITrap      : AI-flavour phrases / patterns that should trigger rewrite.
- GenreProfile    : bundle of default rules + beats for a given genre.
- ToolSpec        : registered tool contract the drafting agent can invoke.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WritingRule(Base):
    __tablename__ = "writing_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    genre = Column(String(64), default="", nullable=False, index=True)
    # pacing | dialogue | hook | description
    category = Column(String(32), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    rule_text = Column(Text, nullable=False)
    examples_json = Column(JSON, default=list, nullable=False)
    priority = Column(Integer, default=50, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class BeatPattern(Base):
    __tablename__ = "beat_patterns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    genre = Column(String(64), default="", nullable=False, index=True)
    # opening | volume_end | climax | turning | closure | ...
    stage = Column(String(32), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="", nullable=False)
    trigger_conditions_json = Column(JSON, default=dict, nullable=False)
    reusable = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class AntiAITrap(Base):
    __tablename__ = "anti_ai_traps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    locale = Column(String(16), default="zh-CN", nullable=False)
    # keyword | regex | ngram
    pattern_type = Column(String(16), nullable=False, index=True)
    pattern = Column(Text, nullable=False)
    # hard | soft
    severity = Column(String(8), default="soft", nullable=False, index=True)
    replacement_hint = Column(Text, default="", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class GenreProfile(Base):
    __tablename__ = "genre_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="", nullable=False)
    default_beat_pattern_ids = Column(JSON, default=list, nullable=False)
    default_writing_rule_ids = Column(JSON, default=list, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ToolSpec(Base):
    __tablename__ = "tool_specs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), unique=True, nullable=False, index=True)
    description = Column(Text, default="", nullable=False)
    input_schema_json = Column(JSON, default=dict, nullable=False)
    output_schema_json = Column(JSON, default=dict, nullable=False)
    # python_callable | sql | qdrant | llm
    handler = Column(String(32), nullable=False)
    config_json = Column(JSON, default=dict, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
