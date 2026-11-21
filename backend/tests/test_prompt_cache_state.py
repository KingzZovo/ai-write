"""Background prompt counters must not outlive the test that created them."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import prompt_cache
from tests.support.prompt_cache_state import clear_prompt_cache_state

pytest_plugins = ["pytester"]


@pytest.mark.asyncio
async def test_cleanup_cancels_and_awaits_flusher_without_database_drain(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("test counters must not be drained"))
    monkeypatch.setattr("app.db.session.async_session_factory", factory)
    started, finished = asyncio.Event(), asyncio.Event()

    async def flusher(_stop):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    monkeypatch.setattr(prompt_cache, "_flusher_loop", flusher)
    await prompt_cache.buffer_track_result(uuid4(), success=True)
    task = prompt_cache._flush_task
    await asyncio.wait_for(started.wait(), timeout=1)
    assert prompt_cache.pending_counts()["success"]

    await clear_prompt_cache_state()

    assert task.done() and finished.is_set()
    assert prompt_cache._flush_task is None
    assert prompt_cache._flush_stop_event is None
    assert prompt_cache.pending_counts() == {"success": {}, "fail": {}}
    factory.assert_not_called()
    await clear_prompt_cache_state()  # Repeated cleanup is harmless.


@pytest.mark.asyncio
async def test_isolation_keeps_real_periodic_flusher_available(monkeypatch):
    flushed = asyncio.Event()

    async def flush():
        flushed.set()
        return {"success": 0, "fail": 0}

    flush_mock = AsyncMock(side_effect=flush)
    monkeypatch.setattr(prompt_cache, "flush_pending_counts", flush_mock)
    monkeypatch.setattr(prompt_cache, "FLUSH_INTERVAL_SECONDS", 0.001)
    prompt_cache.start_flusher()
    await asyncio.wait_for(flushed.wait(), timeout=1)
    task = prompt_cache._flush_task
    assert flush_mock.await_count >= 1
    await clear_prompt_cache_state()
    assert task.done()


def test_shared_fixture_finishes_task_before_restoring_test_boundaries(pytester):
    backend = str(Path(__file__).resolve().parents[1])
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {backend!r})\n"
        "pytest_plugins = ['tests.support.offline', 'tests.support.prompt_cache_state']\n"
    )
    pytester.makeini("""
        [pytest]
        asyncio_mode = strict
        asyncio_default_test_loop_scope = session
        asyncio_default_fixture_loop_scope = session
    """)
    pytester.makepyfile("""
        import asyncio
        import pytest
        from app.services import prompt_cache
        finished = []

        @pytest.mark.asyncio
        async def test_owner(monkeypatch):
            started = asyncio.Event()
            async def flusher(_stop):
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    assert prompt_cache.FLUSH_INTERVAL_SECONDS == 99
                    finished.append(True)
            monkeypatch.setattr(prompt_cache, 'FLUSH_INTERVAL_SECONDS', 99)
            monkeypatch.setattr(prompt_cache, '_flusher_loop', flusher)
            await prompt_cache.buffer_track_result('fake-prompt-id', True)
            await asyncio.wait_for(started.wait(), timeout=1)

        def test_next_owner_starts_clean():
            assert finished == [True]
            assert prompt_cache.FLUSH_INTERVAL_SECONDS == 30
            assert prompt_cache._flush_task is None
            assert prompt_cache.pending_counts() == {'success': {}, 'fail': {}}
    """)
    pytester.runpytest_subprocess("--offline", "-q").assert_outcomes(passed=2)
