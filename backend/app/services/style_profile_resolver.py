"""Resolve a reference book's bound StyleProfile (PR-BOOK-PROFILE-BIND)."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ReferenceBook, StyleProfile


async def get_or_create_book_profile(db: AsyncSession, book_id: str) -> StyleProfile:
    """Return the StyleProfile bound to a reference_book, creating an empty one if missing.

    Raises ValueError if the book itself does not exist.
    """
    book = await db.get(ReferenceBook, UUID(str(book_id)))
    if book is None:
        raise ValueError(f"reference_book {book_id} not found")

    rs = await db.execute(
        select(StyleProfile).where(StyleProfile.source_book_id == book.id).limit(1)
    )
    sp = rs.scalar_one_or_none()
    if sp is not None:
        return sp

    sp = StyleProfile(
        name=f"{book.title} 综合写法",
        source_book=book.title,
        source_book_id=book.id,
        rules_json=[],
        anti_ai_rules=[],
        tone_keywords=[],
        sample_passages=[],
        config_json={},
        bind_level="book",
        is_active=1,
    )
    db.add(sp)
    await db.commit()
    await db.refresh(sp)
    return sp
