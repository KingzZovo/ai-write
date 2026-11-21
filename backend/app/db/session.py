"""Async SQLAlchemy engine and session factory.

v1.13 fix (cross-loop crash on second Celery task):

Previously this module exposed ``engine`` and ``async_session_factory`` as
plain module-level singletons. 21 call sites across the codebase did
``from app.db.session import async_session_factory``, which copies the
reference at import time.

Celery's ``_run_async_safe`` used to "reset" these singletons between tasks
by rewriting attributes on this module, but every previously-imported
reference in caller modules still pointed at the *original* sessionmaker —
bound to the original asyncpg pool, whose connections were created on the
previous task's (now-closed) event loop. Result: every second Celery task
crashed with ``RuntimeError: Future <...> attached to a different loop`` on
its first DB call.

The fix: expose ``async_session_factory`` as a function that always reads
the current sessionmaker via a small mutable state holder. Provide
``reset_engine()`` to drop the cached pool, and
``dispose_current_engine_async()`` to close it on the right loop. All 21
existing call sites are forward-compatible because they already invoke
``async_session_factory()`` as a callable; the call now goes through one
level of indirection that always picks up the freshest engine.
"""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class _State:
    engine: AsyncEngine | None = None
    sessionmaker: "async_sessionmaker[AsyncSession] | None" = None


_state = _State()


def _build() -> None:
    eng = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    sm = async_sessionmaker(
        eng,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _state.engine = eng
    _state.sessionmaker = sm


def _ensure() -> "async_sessionmaker[AsyncSession]":
    if _state.sessionmaker is None:
        _build()
    assert _state.sessionmaker is not None
    return _state.sessionmaker


def async_session_factory() -> AsyncSession:
    """Callable proxy for the current sessionmaker.

    Always returns a session bound to the *current* engine. After
    ``reset_engine()``, subsequent calls use the fresh pool — without
    requiring any caller to re-import.
    """
    return _ensure()()


def reset_engine() -> None:
    """Drop the cached engine and sessionmaker.

    Used by Celery's ``_run_async_safe`` between tasks. Does NOT dispose
    the old engine — that must happen on the original loop via
    ``dispose_current_engine_async()`` *before* the loop is closed, or be
    left to GC. After this call, the next ``async_session_factory()`` call
    will create a fresh engine + pool that binds to whichever loop is
    running at that moment.
    """
    _state.engine = None
    _state.sessionmaker = None


async def dispose_current_engine_async() -> None:
    """Dispose the current engine on the running event loop.

    Safe to call from within ``_run_async_safe``'s business-loop finally
    block: the asyncpg connections were created on this same loop, so
    ``engine.dispose()`` can close them cleanly. After dispose, the
    cached engine is cleared; the next ``async_session_factory()`` call
    lazily builds a new one bound to the next loop.
    """
    eng = _state.engine
    _state.engine = None
    _state.sessionmaker = None
    if eng is not None:
        try:
            await eng.dispose()
        except Exception:
            # Best-effort: stale connections from a prior loop will be
            # garbage-collected. Failing to dispose is not fatal.
            pass


def __getattr__(name: str) -> Any:
    """Backward-compat module attribute access for ``engine``.

    A few callers do ``from app.db.session import engine`` (or read
    ``_ses.engine`` from elsewhere). Resolve to the current engine,
    building one lazily if necessary.
    """
    if name == "engine":
        if _state.engine is None:
            _build()
        return _state.engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
