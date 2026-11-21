"""Endpoint publication and router reload races, without model requests."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import model_config
from app.models.project import LLMEndpoint
from app.services import model_router
from tests.test_pipeline_control import runtime_db


@pytest.mark.asyncio
async def test_endpoint_crud_commits_before_router_reset(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(LLMEndpoint.__table__.create)
    async with r.sessions() as db:
        resets = []

        def reset():
            assert not db.in_transaction()
            resets.append(True)

        monkeypatch.setattr(model_config, "reset_model_router", reset)
        created = await model_config.create_endpoint(model_config.EndpointCreate(
            name="测试", provider_type="openai_compatible",
            base_url="http://localhost:8317/v1", api_key="test-only-key",
            default_model="old-model",
        ), db)
        async with r.sessions() as reader:
            row = await reader.get(LLMEndpoint, created.id)
            assert row.default_model == "old-model"
            stored_key = row.api_key
        updated = await model_config.update_endpoint(created.id,
            model_config.EndpointUpdate(default_model="new-model"), db)
        assert updated.default_model == "new-model"
        async with r.sessions() as reader:
            row = await reader.get(LLMEndpoint, created.id)
            assert row.default_model == "new-model"
            assert row.api_key == stored_key
        await model_config.delete_endpoint(created.id, db)
        async with r.sessions() as reader:
            assert await reader.get(LLMEndpoint, created.id) is None
        assert len(resets) == 3


@pytest.mark.asyncio
async def test_endpoint_failed_commit_does_not_reset_router(monkeypatch):
    reset = MagicMock()
    monkeypatch.setattr(model_config, "reset_model_router", reset)
    db = AsyncMock(commit=AsyncMock(side_effect=RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        await model_config._commit_endpoint_change(db)
    reset.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_reader", [False, True])
async def test_reset_during_load_cannot_return_stale_or_missing_router(monkeypatch, fresh_reader):
    monkeypatch.setattr(model_router, "_router", None)
    monkeypatch.setattr(model_router, "_router_locks", {})
    started, resume = asyncio.Event(), asyncio.Event()
    candidates = []

    async def load(self):
        candidates.append(self)
        if len(candidates) == 1:
            started.set()
            await resume.wait()
        self.providers["test"] = object()
        self._db_loaded = True

    monkeypatch.setattr(model_router.ModelRouter, "load_from_db", load)
    pending = asyncio.create_task(model_router.get_model_router_async())
    try:
        await asyncio.wait_for(started.wait(), 2)
        model_router.reset_model_router()
        fresh = await model_router.get_model_router_async() if fresh_reader else None
        resume.set()
        result = await asyncio.wait_for(pending, 2)
        assert len(candidates) == 2
        assert result is candidates[1] is model_router._router
        if fresh_reader:
            assert result is fresh
        assert await model_router.get_model_router_async() is result
    finally:
        resume.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
