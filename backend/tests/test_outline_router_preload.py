"""Regression: outline generation must use a DB-loaded model router.

Root cause (2026-06-25, reproduced on live): ``OutlineGenerator.__init__``
grabbed the router via the SYNC ``get_model_router()``. Inside the running
SSE event loop that getter hits the ``loop.is_running()`` branch and returns
WITHOUT loading DB config — it only falls back to env providers. This
deployment is DB-configured (env LLM keys are empty), so the router had zero
providers and ``_get_route`` raised::

    No model configured for 'outline_book'. Configure at Settings > ...

emitted into the SSE stream as an ``{"error": ...}`` event — the
"大纲生成回退到 prompt 提示词" symptom. Every other generation path
(chapter etc.) uses ``run_text_prompt`` → ``get_model_router_async`` which
force-loads the DB, so only outline was affected.

The fix: ``generate_outline`` awaits ``get_model_router_async()`` before
instantiating ``OutlineGenerator``, healing the shared singleton so the
sync getter inside ``__init__`` returns the same DB-loaded instance. These
tests pin that the outline API preloads the router.
"""
from __future__ import annotations

import inspect

import pytest


def test_generate_outline_preloads_async_router() -> None:
    """The outline SSE handler must call get_model_router_async so the
    shared router singleton is DB-loaded before OutlineGenerator grabs it
    via the sync getter."""
    import app.api.generate as gen_mod

    src = inspect.getsource(gen_mod.generate_outline)
    assert "get_model_router_async" in src, (
        "generate_outline must await get_model_router_async() before "
        "instantiating OutlineGenerator, or outline routing silently "
        "falls back to the empty env provider inside the SSE event loop."
    )


@pytest.mark.asyncio
async def test_async_router_load_heals_sync_getter(monkeypatch) -> None:
    """After get_model_router_async loads the DB, the sync get_model_router
    must return the SAME loaded singleton (proves the preload fix works)."""
    import app.services.model_router as mr

    # Simulate the failure precondition: singleton reset / never loaded.
    monkeypatch.setattr(mr, "_router", None, raising=False)

    async def fake_load(self) -> None:
        # Stand in for a DB load that registers one chat provider.
        self.providers["ep-test"] = object()
        self.task_routing["outline_book"] = mr.TaskRouteConfig(
            provider_key="ep-test", model_name=""
        )
        self._db_loaded = True

    monkeypatch.setattr(mr.ModelRouter, "load_from_db", fake_load)

    loaded = await mr.get_model_router_async()
    assert loaded.providers, "async loader must populate providers"

    # The sync getter (used by OutlineGenerator.__init__) must now return
    # the same healed singleton — not a fresh env-only empty router.
    sync_router = mr.get_model_router()
    assert sync_router is loaded
    assert sync_router.providers, "sync getter must see the DB-loaded providers"
