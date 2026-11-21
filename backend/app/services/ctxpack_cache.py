"""v0.9: Context Pack invalidation via Redis.

Writes ``ctxpack:invalid:{project_id}`` = "1" (with TTL) whenever settings
change. ``ContextPackBuilder.build()`` should check this before serving a
cached pack and rebuild from scratch when the flag is present.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_KEY_TEMPLATE = "ctxpack:invalid:{project_id}"
_DEFAULT_TTL_SECONDS = 3600  # 1h safety so cleared runtimes won't keep flag forever


async def _client() -> Any:
    try:
        from app.db.redis import _client as redis_client  # type: ignore[attr-defined]

        return redis_client
    except Exception:
        return None


def _key(project_id: str | Any) -> str:
    return _KEY_TEMPLATE.format(project_id=str(project_id))


async def invalidate(project_id: str | Any, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> bool:
    """Mark the project's context pack as invalid. Best-effort; returns True if the flag was written."""
    client = await _client()
    if client is None:
        logger.debug("ctxpack_cache.invalidate skipped: redis client is None")
        return False
    try:
        await client.setex(_key(project_id), int(ttl_seconds), "1")
        return True
    except Exception as exc:
        logger.warning("ctxpack_cache.invalidate failed for %s: %s", project_id, exc)
        return False


async def is_invalid(project_id: str | Any) -> bool:
    """Return True if the project's context pack cache should be bypassed."""
    client = await _client()
    if client is None:
        return False
    try:
        value = await client.get(_key(project_id))
        return bool(value)
    except Exception as exc:
        logger.warning("ctxpack_cache.is_invalid failed for %s: %s", project_id, exc)
        return False


async def clear(project_id: str | Any) -> bool:
    """Clear the invalidation flag (call after a successful rebuild)."""
    client = await _client()
    if client is None:
        return False
    try:
        await client.delete(_key(project_id))
        return True
    except Exception as exc:
        logger.warning("ctxpack_cache.clear failed for %s: %s", project_id, exc)
        return False
