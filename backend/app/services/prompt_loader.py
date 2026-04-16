"""Prompt Loader — resolves prompts from PromptRegistry with hardcoded fallback.

Usage in any service:
    system_prompt = await load_prompt("generation", fallback=PLOT_AGENT_SYSTEM)

This bridges the gap between hardcoded prompts and the PromptRegistry without
requiring all services to be rewritten at once.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Cache to avoid DB queries on every call
_cache: dict[str, str] = {}


async def load_prompt(task_type: str, fallback: str = "") -> str:
    """Load a prompt from the PromptRegistry, falling back to hardcoded string.

    Results are cached in-memory for the process lifetime.
    """
    if task_type in _cache:
        return _cache[task_type]

    try:
        from app.db.session import async_session_factory
        from app.services.prompt_registry import PromptRegistry

        async with async_session_factory() as db:
            registry = PromptRegistry(db)
            asset = await registry.get(task_type)
            if asset and asset.system_prompt:
                _cache[task_type] = asset.system_prompt
                return asset.system_prompt
    except Exception as e:
        logger.debug("PromptRegistry lookup failed for %s: %s", task_type, e)

    _cache[task_type] = fallback
    return fallback


def clear_cache() -> None:
    """Clear the prompt cache (e.g., after editing a prompt in the UI)."""
    _cache.clear()
