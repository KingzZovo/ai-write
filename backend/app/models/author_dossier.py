"""Author-level dossier — one consolidated profile per reference-book author.

Pyramid principle: the author tier consumes the per-book dossiers stored in
``ReferenceBook.metadata_json['dossier']`` (never the raw micro-cards) and
merges them into one author dossier distinguishing 「作者惯用」 (patterns
consistent across the author's books) from 「单书特例」 (book-specific,
labeled 《书N》 with a ``book_labels`` map back to real titles).

One row per author (``author`` is unique). ``status_json`` carries the same
polling marker shape as the per-book ``dossier_status``
(``{state, updated_at, llm_calls}``); ``dossier_json`` mirrors the book
dossier contract with slightly larger block caps;
``source_book_ids_json`` records which reference books fed the merge.
"""

import uuid

from sqlalchemy import Column, DateTime, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base
from app.models.project import _utcnow


class AuthorDossier(Base):
    """Consolidated cross-book dossier for one reference-book author."""

    __tablename__ = "author_dossiers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    author = Column(String(200), nullable=False, unique=True)
    status_json = Column(JSON, default=dict)         # {state, updated_at, llm_calls}
    dossier_json = Column(JSON, default=dict)        # book-dossier-shaped contract
    source_book_ids_json = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
