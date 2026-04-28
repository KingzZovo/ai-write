"""v1.7.1 Z1: ensure router passes task_type into provider call kwargs.

Before Z1, provider's `kw.get("task_type", "unknown")` always returned
"unknown" because router.generate did not propagate task_type into the
provider call's **kw, polluting Prometheus llm_cache_token_total with a
single high-cardinality 'unknown' bucket.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services.model_router import (
    BaseProvider,
    GenerationResult,
    ModelRouter,
    TaskRouteConfig,
    TokenUsage,
)


class _CapturingProvider(BaseProvider):
    name = "capture"

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}

    async def generate(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.last_kwargs = dict(kw)
        return GenerationResult(text="ok", usage=TokenUsage(0, 0, 0), model=model, provider=self.name)

    async def generate_stream(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.last_kwargs = dict(kw)
        for ch in ("o", "k"):
            yield ch


def _build_router_with_capture() -> tuple[ModelRouter, _CapturingProvider]:
    """Construct a real ModelRouter and patch routing to a capturing provider."""
    cap = _CapturingProvider()
    router = ModelRouter()  # real init seeds all needed dicts
    router.providers["capture"] = cap
    router._endpoint_defaults["capture"] = "test-model"
    router._endpoint_tiers["capture"] = "standard"
    router.task_routing["unit_test_task"] = TaskRouteConfig(
        provider_key="capture", model_name="test-model",
        temperature=0.5, max_tokens=128,
    )
    return router, cap


def test_router_generate_propagates_task_type_into_provider_kwargs() -> None:
    router, cap = _build_router_with_capture()
    asyncio.run(router.generate(task_type="unit_test_task", messages=[{"role": "user", "content": "hi"}]))
    assert cap.last_kwargs.get("task_type") == "unit_test_task", (
        f"expected task_type='unit_test_task' in provider **kw, got {cap.last_kwargs!r}"
    )


def test_router_generate_stream_propagates_task_type_into_provider_kwargs() -> None:
    router, cap = _build_router_with_capture()

    async def _drain() -> None:
        async for _ in router.generate_stream(task_type="unit_test_task", messages=[{"role": "user", "content": "hi"}]):
            pass

    asyncio.run(_drain())
    assert cap.last_kwargs.get("task_type") == "unit_test_task"


def test_generate_by_route_signature_has_task_type_default() -> None:
    """generate_by_route accepts task_type kwarg with sane default 'by_route'."""
    import inspect

    sig = inspect.signature(ModelRouter.generate_by_route)
    assert "task_type" in sig.parameters, sig
    assert sig.parameters["task_type"].default == "by_route"
