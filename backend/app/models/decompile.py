"""v0.6 — Reference-book decompile artifacts.

Semantic slices, style profile cards, beat sheet cards. Mirror Qdrant
points so they can be browsed/edited/deleted from the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class ReferenceBookSlice(Base):
    """A semantic slice of a reference book (scene / paragraph / dialogue unit).

    Unlike TextChunk (which is length-bounded), slices respect natural
    boundaries so downstream abstractors receive complete scenes.
    """

    __tablename__ = "reference_book_slices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slice_type = Column(String(30), nullable=False, default="scene")  # scene|paragraph|dialogue|chapter
    chapter_idx = Column(Integer, nullable=True)
    sequence_id = Column(Integer, nullable=False, default=0)  # order within book
    start_offset = Column(Integer, nullable=False, default=0)
    end_offset = Column(Integer, nullable=False, default=0)
    raw_text = Column(Text, nullable=False)  # kept for regeneration / debugging
    token_count = Column(Integer, nullable=False, default=0)
    meta_json = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    style_cards = relationship(
        "StyleProfileCard",
        back_populates="slice",
        cascade="all, delete-orphan",
    )
    beat_cards = relationship(
        "BeatSheetCard",
        back_populates="slice",
        cascade="all, delete-orphan",
    )


class StyleProfileCard(Base):
    """Structured style profile extracted from a slice (no raw text leakage).

    Mirrors a Qdrant point in the `style_profiles` collection. Stored in
    Postgres so the UI can browse/edit/tag without hitting Qdrant.
    """

    __tablename__ = "style_profile_cards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slice_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_book_slices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_json = Column(JSON, nullable=False, default=dict)
    qdrant_point_id = Column(String(64), nullable=True)  # int-as-string for cross-db portability
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    slice = relationship("ReferenceBookSlice", back_populates="style_cards")


class BeatSheetCard(Base):
    """Abstracted plot beat (entity-stripped) extracted from a slice."""

    __tablename__ = "beat_sheet_cards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_books.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slice_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reference_book_slices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    beat_json = Column(JSON, nullable=False, default=dict)
    qdrant_point_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    slice = relationship("ReferenceBookSlice", back_populates="beat_cards")
