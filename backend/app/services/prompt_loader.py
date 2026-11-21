"""Compatibility prompt loader backed by the shared, invalidatable TTL cache.

Fallbacks belong to each caller: never cache one caller's fallback as another
caller's registered prompt. Closing the read session precedes all model IO.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def load_prompt(task_type: str, fallback: str = "") -> str:
    from app.db.session import async_session_factory
    from app.services import prompt_cache

    try:
        async with async_session_factory() as db:
            snapshot = await prompt_cache.get_snapshot(task_type, db)
        if snapshot and snapshot.system_prompt:
            return snapshot.system_prompt
    except Exception as exc:
        logger.debug("Prompt cache lookup failed for %s: %s", task_type, exc)
    return fallback


def clear_cache() -> None:
    """Compatibility API; UI invalidation and legacy callers share one cache."""
    from app.services.prompt_cache import invalidate
    invalidate()
