"""Actual Registry use of a persisted logical route, including strict JSON."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import prompt_cache, prompt_registry
from app.services.prompt_execution_snapshot import capture_prompt_snapshot, restore_prompt_snapshot
from tests.test_arc_planning_journal import route


@pytest.mark.asyncio
async def test_saved_prompt_and_budget_reach_router_even_after_current_registry_changes(monkeypatch):
    original = route("arc_outline_meta")
    original["system_prompt"] = "保留作者原设定。不要凭模型偏好另造冲突。"
    original["max_tokens"] = 16384
    router = SimpleNamespace(generate_with_tier_fallback=AsyncMock(return_value=SimpleNamespace(
        text='{"chapter_count":4}', finish_reason="stop")))
    current = AsyncMock(side_effect=AssertionError("must not resolve new registry config during retry"))
    monkeypatch.setattr(prompt_registry, "get_model_router_async", AsyncMock(return_value=router))
    monkeypatch.setattr(prompt_registry, "_resolve_route_and_tier_cached", current)
    monkeypatch.setattr(prompt_cache, "buffer_track_result", AsyncMock())
    result = await prompt_registry.run_structured_prompt("arc_outline_meta", "完整原作者材料", None,
        frozen_prompt=original, strict_json=True, retry_on_validation_error=False, temperature=0.0)
    assert result == {"chapter_count": 4}
    args, kwargs = router.generate_with_tier_fallback.await_args
    assert args[1] == [{"role": "system", "content": original["system_prompt"]}, {"role": "user", "content": "完整原作者材料"}]
    assert kwargs["route"].max_tokens == 16384 and kwargs["route"].model == original["model"]
    assert kwargs["temperature"] == 0.0 and "frozen_prompt" not in kwargs
    assert current.await_count == 0


@pytest.mark.asyncio
async def test_capture_uses_cached_resolver_and_contains_no_endpoint_secrets(monkeypatch):
    original = route("arc_outline_batch")
    spec, tier = restore_prompt_snapshot(original, "arc_outline_batch")
    cached = AsyncMock(return_value=(spec, tier))
    monkeypatch.setattr(prompt_registry, "_resolve_route_and_tier_cached", cached)
    assert await capture_prompt_snapshot("arc_outline_batch", object()) == original
    assert not {"api_key", "base_url", "headers", "password"}.intersection(original)
    assert cached.await_count == 1


@pytest.mark.parametrize("field,value", [("task_type", "different-task"), ("max_tokens", True),
    ("max_tokens", 0), ("temperature", float('nan')), ("system_prompt", ""),
    ("endpoint_id", "unknown"), ("api_key", "must-not-be-accepted")])
def test_damaged_or_cross_task_snapshot_is_not_a_fallback_to_current_config(field, value):
    saved = deepcopy(route("arc_outline_meta")); saved[field] = value
    with pytest.raises((ValueError, TypeError)):
        restore_prompt_snapshot(saved, "arc_outline_meta")
