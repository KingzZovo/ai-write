"""Keep process-local prompt state and its background task inside each test."""
import asyncio
from contextlib import suppress

import pytest_asyncio

from app.services import prompt_cache


async def clear_prompt_cache_state() -> None:
    # stop_flusher() deliberately drains to the database in production.
    # Test counters belong to fake prompt IDs: cancel and await the task
    # instead, then discard that test's state without performing a drain.
    task = prompt_cache._flush_task
    try:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        prompt_cache._flush_task = None
        prompt_cache._flush_stop_event = None
        prompt_cache.reset_for_tests()


@pytest_asyncio.fixture(autouse=True)
async def isolated_prompt_cache(monkeypatch):
    # Keep monkeypatch-owned model/DB boundaries installed until cancellation
    # has completed, including asynchronous finalizers in the flusher.
    await clear_prompt_cache_state()
    try:
        yield
    finally:
        await clear_prompt_cache_state()
