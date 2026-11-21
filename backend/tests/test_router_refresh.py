"""Bounded router refresh, retained references and in-flight isolation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import model_router as mr, prompt_cache


class ProbeProvider:
    async def generate(self, messages, model, **kwargs):
        return mr.GenerationResult(model, mr.TokenUsage(total_tokens=1), model, "test")

    async def generate_stream(self, messages, model, **kwargs):
        yield model

    async def embed(self, text):
        return [2.0]


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setattr(mr, "_router", None)
    monkeypatch.setattr(mr, "_router_locks", {})
    state = SimpleNamespace(model="old", key="same-key", loads=0, error=False,
                            snapshots=("old prompt",))

    async def load(self):
        state.loads += 1
        if state.error:
            raise RuntimeError("configuration unavailable")
        self.providers["test"] = ProbeProvider()
        self._provider_configs["test"] = (state.key,)
        self._endpoint_defaults["test"] = state.model
        self._endpoint_tiers["test"] = "standard"
        self.task_routing["generation"] = mr.TaskRouteConfig("test", "")
        self.embedding_provider = ProbeProvider()
        self._embedding_config = (state.key, "embedding")
        self._prompt_snapshots = state.snapshots
        self._has_db_endpoints = True
        self._db_loaded = True

    monkeypatch.setattr(mr.ModelRouter, "load_from_db", load)
    return state


def expire(router):
    router._loaded_at -= mr.ROUTER_TTL_SECONDS + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "generate", "generate_stream", "generate_by_route", "stream_by_route",
    "generate_with_fallback", "generate_with_tier_fallback", "stream_with_tier_fallback",
])
async def test_retained_reference_refreshes_before_each_dispatch(managed, method):
    old = await mr.get_model_router_async()
    managed.model = "new"
    assert await mr.get_model_router_async() is old  # Warm cache is read-free.
    assert managed.loads == 1
    expire(old)
    route = SimpleNamespace(endpoint_id="test", model="", temperature=0.4, max_tokens=32)
    args = {"messages": [{"role": "user", "content": "test"}]}
    if method in {"generate_by_route", "stream_by_route"}:
        args["route"] = route
    else:
        args["task_type"] = "generation"
    call = getattr(old, method)(**args)
    if "stream" in method:
        assert [chunk async for chunk in call] == ["new"]
    else:
        assert (await call).text == "new"
    assert managed.loads == 2
    assert mr._router is not old
    assert old._endpoint_defaults["test"] == "old"


@pytest.mark.asyncio
async def test_embedding_reference_refreshes_and_unchanged_clients_are_reused(managed):
    old = await mr.get_model_router_async()
    old.total_usage.total_tokens = 12
    expire(old)
    assert await old.embed("test") == [2.0]
    fresh = mr._router
    assert fresh.providers["test"] is old.providers["test"]
    assert fresh.embedding_provider is old.embedding_provider
    assert fresh.total_usage is old.total_usage
    managed.key = "rotated-key"
    expire(fresh)
    rotated = await mr.get_model_router_async()
    assert rotated.providers["test"] is not fresh.providers["test"]
    assert rotated.embedding_provider is not fresh.embedding_provider


@pytest.mark.asyncio
async def test_failed_refresh_does_not_call_old_provider_or_environment(managed, monkeypatch):
    old = await mr.get_model_router_async()
    expire(old)
    managed.error = True
    env = MagicMock()
    monkeypatch.setattr(mr, "_load_from_env", env)
    with pytest.raises(RuntimeError, match="configuration unavailable"):
        await old.generate("generation", [])
    assert mr._router is None
    assert old.total_usage.total_tokens == 0
    env.assert_not_called()
    managed.error = False
    managed.model = "recovered"
    assert (await old.generate("generation", [])).text == "recovered"


@pytest.mark.asyncio
async def test_cancelled_load_is_not_published(managed, monkeypatch):
    original = mr.ModelRouter.load_from_db
    started = asyncio.Event()

    async def suspended(self):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mr.ModelRouter, "load_from_db", suspended)
    task = asyncio.create_task(mr.get_model_router_async())
    try:
        await asyncio.wait_for(started.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert mr._router is None
    monkeypatch.setattr(mr.ModelRouter, "load_from_db", original)
    assert (await mr.get_model_router_async())._db_loaded


@pytest.mark.asyncio
async def test_parallel_expiry_performs_one_reload(managed):
    old = await mr.get_model_router_async()
    expire(old)
    results = await asyncio.gather(*(mr.get_model_router_async() for _ in range(20)))
    assert managed.loads == 2
    assert all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_only_changed_prompt_snapshot_invalidates_cache(managed, monkeypatch):
    invalidate = MagicMock()
    monkeypatch.setattr(prompt_cache, "invalidate", invalidate)
    old = await mr.get_model_router_async()
    assert invalidate.call_count == 1
    expire(old)
    new = await mr.get_model_router_async()
    assert invalidate.call_count == 1
    managed.snapshots = ("new prompt",)
    expire(new)
    await mr.get_model_router_async()
    assert invalidate.call_count == 2


@pytest.mark.asyncio
async def test_active_stream_keeps_snapshot_while_next_call_refreshes(managed):
    old = await mr.get_model_router_async()
    resume = asyncio.Event()

    async def stream(messages, model, **kwargs):
        yield model + "-start"
        await resume.wait()
        yield model + "-end"

    old.providers["test"].generate_stream = stream
    active = old.generate_stream("generation", [])
    try:
        assert await anext(active) == "old-start"
        managed.model = "new"
        expire(old)
        assert (await old.generate("generation", [])).text == "new"
        resume.set()
        assert await anext(active) == "old-end"
    finally:
        resume.set()
        await active.aclose()


@pytest.mark.asyncio
async def test_disabled_database_endpoints_do_not_reenable_environment(managed, monkeypatch):
    async def load(self):
        self._has_db_endpoints = True
        self._db_loaded = True

    monkeypatch.setattr(mr.ModelRouter, "load_from_db", load)
    env = MagicMock()
    monkeypatch.setattr(mr, "_load_from_env", env)
    router = await mr.get_model_router_async()
    assert router.providers == {}
    env.assert_not_called()
    with pytest.raises(ValueError, match="No model configured"):
        await router.generate("generation", [])
