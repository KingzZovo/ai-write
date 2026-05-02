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
from sqlalchemy import Boolean
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
    target_word_count = Column(
        Integer, nullable=False, server_default="3000000", default=3000000
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

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
    target_word_count = Column(
        Integer, nullable=False, server_default="200000", default=200000
    )
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
    target_word_count = Column(
        Integer, nullable=False, server_default="50000", default=50000
    )

    volume = relationship("Volume", back_populates="chapters")
    versions = relationship(
        "ChapterVersion",
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="ChapterVersion.created_at",
    )
    evaluations = relationship(
        "ChapterEvaluation",
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="ChapterEvaluation.created_at",
    )


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
    parent_outline = relationship(
        "Outline", remote_side="Outline.id", backref="children"
    )


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
    """A writing style profile that can be bound to a book/chapter/generation."""

    __tablename__ = "style_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    source_book = Column(String(500))

    # Core style rules
    rules_json = Column(JSON, default=list)         # [{rule, weight, category}]
    anti_ai_rules = Column(JSON, default=list)      # [{pattern, replacement, autoRewrite}]
    tone_keywords = Column(JSON, default=list)      # Style keyword tags
    sample_passages = Column(JSON, default=list)    # Few-shot example passages

    # Binding: global / book / chapter
    bind_level = Column(String(20), default="global")
    bind_target_id = Column(UUID(as_uuid=True), nullable=True)

    # Status
    is_active = Column(Integer, default=1)
    config_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class LLMEndpoint(Base):
    """A configured LLM API endpoint (user-managed from frontend)."""

    __tablename__ = "llm_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)  # User-friendly name, e.g. "Claude API", "Local Qwen", "Jina Embedding"
    provider_type = Column(String(50), nullable=False)  # anthropic, openai, openai_compatible
    base_url = Column(String(1000), default="")  # Only for openai_compatible
    api_key = Column(String(500), default="")  # Encrypted in production
    default_model = Column(String(200), nullable=False)  # e.g. "claude-sonnet-4-20250514", "gpt-4o", "text-embedding-3-small"
    # v1.4 — tiering label for routing matrix
    tier = Column(String(20), nullable=False, default="standard")
    enabled = Column(Integer, default=1)
    last_test_ok = Column(Integer, default=0)
    last_test_latency = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


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


# =============================================================================
# Phase 2: Knowledge Base & Style Learning
# =============================================================================


class BookSource(Base):
    """Legado-compatible book source rule definition with health tracking."""

    __tablename__ = "book_sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    source_url = Column(String(1000), nullable=False)
    source_type = Column(Integer, default=0)  # 0=text
    source_group = Column(String(200))
    source_json = Column(JSON, nullable=False)  # full legado BookSource JSON
    enabled = Column(Integer, default=1)
    last_test_at = Column(DateTime(timezone=True))
    last_test_ok = Column(Integer, default=0)
    # Health & scoring
    score = Column(Float, default=5.0)  # 0-10, auto-adjusted
    success_count = Column(Integer, default=0)  # successful fetches
    fail_count = Column(Integer, default=0)  # failed fetches
    consecutive_fails = Column(Integer, default=0)  # consecutive failures (auto-disable at 5)
    avg_quality = Column(Float, default=0.0)  # average content quality
    total_books_fetched = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class ReferenceBook(Base):
    """A reference novel imported for style learning."""

    __tablename__ = "reference_books"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    author = Column(String(200))
    source = Column(String(50), nullable=False)  # crawler, upload_txt, upload_epub, upload_html, api
    source_detail = Column(String(1000))  # URL or filename
    total_chapters = Column(Integer, default=0)
    total_words = Column(Integer, default=0)
    status = Column(String(20), default="pending")  # pending, crawling, cleaning, extracting, ready, error
    error_message = Column(Text)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    chunks = relationship("TextChunk", back_populates="book", cascade="all, delete-orphan")


class TextChunk(Base):
    """A text block from a reference book after cleaning and slicing."""

    __tablename__ = "text_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="CASCADE"),
        nullable=False,
    )
    chapter_idx = Column(Integer, nullable=False)
    block_idx = Column(Integer, nullable=False)
    chapter_title = Column(String(500))
    content = Column(Text, nullable=False)
    char_count = Column(Integer, nullable=False)
    sequence_id = Column(Integer, nullable=False)  # global sequential ID within book
    plot_extracted = Column(Integer, default=0)
    style_extracted = Column(Integer, default=0)
    style_features_json = Column(JSON, nullable=True)
    plot_features_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    book = relationship("ReferenceBook", back_populates="chunks")


class CrawlTask(Base):
    """A crawling task for fetching novel content."""

    __tablename__ = "crawl_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id = Column(UUID(as_uuid=True), ForeignKey("book_sources.id"), nullable=True)
    book_url = Column(String(1000), nullable=False)
    total_chapters = Column(Integer, default=0)
    completed_chapters = Column(Integer, default=0)
    status = Column(String(20), default="pending")  # pending, running, paused, completed, error
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# =============================================================================
# Phase 3: Quality Evaluation & Version Control
# =============================================================================


class ChapterVersion(Base):
    """A version node in the chapter version tree."""

    __tablename__ = "chapter_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapter_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    branch_name = Column(String(100), default="main")
    content_text = Column(Text, nullable=False)
    content_diff = Column(Text, default="")
    word_count = Column(Integer, default=0)
    is_active = Column(Integer, default=0)
    source = Column(String(50), default="user_edit")  # ai_generation, user_edit, merge, branch
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    chapter = relationship("Chapter", back_populates="versions")
    parent_version = relationship(
        "ChapterVersion",
        remote_side="ChapterVersion.id",
        backref="children",
    )


class ChapterEvaluation(Base):
    """Quality evaluation result for a chapter."""

    __tablename__ = "chapter_evaluations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    plot_coherence = Column(Float, default=0.0)
    character_consistency = Column(Float, default=0.0)
    style_adherence = Column(Float, default=0.0)
    narrative_pacing = Column(Float, default=0.0)
    foreshadow_handling = Column(Float, default=0.0)
    overall = Column(Float, default=0.0)
    issues_json = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    chapter = relationship("Chapter", back_populates="evaluations")


class FilterWord(Base):
    """Configurable filter words for Anti-AI detection and style control."""

    __tablename__ = "filter_words"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    word = Column(String(100), nullable=False, unique=True)
    category = Column(String(50), nullable=False)  # ai_trace, cliche, banned, custom
    severity = Column(String(20), default="medium")  # low, medium, high
    replacement = Column(String(200), default="")  # suggested replacement
    source = Column(String(20), default="builtin")  # builtin, user, ai_detected
    enabled = Column(Integer, default=1)
    hit_count = Column(Integer, default=0)  # how many times detected
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Relationship(Base):
    __tablename__ = "relationships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    rel_type = Column(String(50), nullable=False)
    label = Column(String(200), default="")
    note = Column(Text, default="")
    sentiment = Column(String(20), default="neutral")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    # v0.9: relationship evolution across volumes
    since_volume_id = Column(
        UUID(as_uuid=True),
        ForeignKey("volumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    until_volume_id = Column(
        UUID(as_uuid=True),
        ForeignKey("volumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    evolution_json = Column(JSON, default=list)  # [{volume_id, label, sentiment, note}]


class ChapterVariant(Base):
    """BVSR candidate draft for a chapter (v1.0).

    One row per N-draft run. ``is_winner`` flags the variant that critic picked.
    ``selected_by_user`` means the author later manually overrode the winner.
    """

    __tablename__ = "chapter_variants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chapter_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    variant_idx = Column(Integer, nullable=False)
    content_text = Column(Text, nullable=False)
    word_count = Column(Integer, default=0)
    score = Column(Float, nullable=True)
    hard_count = Column(Integer, default=0)
    soft_count = Column(Integer, default=0)
    ai_trap_count = Column(Integer, default=0)
    critic_report_json = Column(JSON, default=dict)
    is_winner = Column(Boolean, default=False)
    selected_by_user = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
