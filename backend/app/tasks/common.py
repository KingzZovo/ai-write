"""Shared helpers for Celery task modules."""

import asyncio
import logging

logger = logging.getLogger(__name__)

def _run_async(coro):
    """Run async function in sync Celery task context.

    v1.7 X2: delegates to the unified _run_async_safe from app.tasks,
    which calls reset_model_router + reset_engine before the new loop and
    dispose_current_engine_async in the finally block. This unifies the
    8 call-sites here with the rest of the codebase and guarantees the
    same loop-bound cache hygiene used by tasks/__init__.py.
    """
    from app.tasks import _run_async_safe
    return _run_async_safe(coro)


def _make_session():
    """Create a fresh async session factory for Celery tasks (avoids event loop conflicts)."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.config import settings
    eng = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=3)
    return async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)


