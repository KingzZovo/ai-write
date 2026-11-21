"""Session ownership for generation context snapshots.

Passing a session preserves the caller's transaction, including pending writes.
Passing None owns a short session which closes before the caller starts its LLM.
"""
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory


@asynccontextmanager
async def generation_context_session(db: AsyncSession | None) -> AsyncIterator[AsyncSession]:
    if db is not None:
        yield db
    else:
        async with async_session_factory() as owned_db:
            yield owned_db
