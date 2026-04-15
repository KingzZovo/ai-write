"""ORM models for the AI writing platform."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    genre = Column(String(100))
    premise = Column(Text)
    settings_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # relationships
    volumes = relationship(
        "Volume",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Volume.volume_idx",
    )
    outlines = relationship(
        "Outline", back_populates="project", cascade="all, delete-orphan"
    )
    characters = relationship(
        "Character", back_populates="project", cascade="all, delete-orphan"
    )
    world_rules = relationship(
        "WorldRule", back_populates="project", cascade="all, delete-orphan"
    )


class Volume(Base):
    __tablename__ = "volumes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(500), nullable=False)
    volume_idx = Column(Integer, nullable=False)
    summary = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    project = relationship("Project", back_populates="volumes")
    chapters = relationship(
        "Chapter",
        back_populates="volume",
        cascade="all, delete-orphan",
        order_by="Chapter.chapter_idx",
    )


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    volume_id = Column(
        UUID(as_uuid=True),
        ForeignKey("volumes.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(500), nullable=False)
    chapter_idx = Column(Integer, nullable=False)
    outline_json = Column(JSON, default=dict)
    content_text = Column(Text, default="")
    word_count = Column(Integer, default=0)
    status = Column(String(20), default="draft")
    summary = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    volume = relationship("Volume", back_populates="chapters")


class Outline(Base):
    __tablename__ = "outlines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    level = Column(String(20), nullable=False)
    parent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
    )
    content_json = Column(JSON, default=dict)
    version = Column(Integer, default=1)
    is_confirmed = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    project = relationship("Project", back_populates="outlines")
    children = relationship("Outline", backref="parent", remote_side=[id])


class Character(Base):
    __tablename__ = "characters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(200), nullable=False)
    profile_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    project = relationship("Project", back_populates="characters")


class WorldRule(Base):
    __tablename__ = "world_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    category = Column(String(100), nullable=False)
    rule_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    project = relationship("Project", back_populates="world_rules")


class StyleProfile(Base):
    __tablename__ = "style_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    source_book = Column(String(500))
    config_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type = Column(String(50), nullable=False)
    provider = Column(String(50), nullable=False)
    model_name = Column(String(200), nullable=False)
    params_json = Column(JSON, default=dict)
    is_active = Column(Integer, default=1)


class Foreshadow(Base):
    __tablename__ = "foreshadows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(String(20), nullable=False)
    description = Column(Text, nullable=False)
    planted_chapter = Column(Integer, nullable=False)
    resolve_conditions_json = Column(JSON, default=list)
    resolution_blueprint_json = Column(JSON, default=dict)
    narrative_proximity = Column(Float, default=0.0)
    status = Column(String(20), default="planted")
    resolved_chapter = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class VolumeSummary(Base):
    __tablename__ = "volume_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    volume_id = Column(
        UUID(as_uuid=True),
        ForeignKey("volumes.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary_text = Column(Text, nullable=False)
    character_snapshot_json = Column(JSON, default=dict)
    plot_progress_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
