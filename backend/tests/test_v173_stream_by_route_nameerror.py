"""v1.7.3 hotfix: stream_by_route must not NameError when _log_meta is None.

Before v1.7.3 the ``_log_meta is None`` branch of
``ModelRouter.stream_by_route`` referenced an undefined ``task_type``
identifier (the method signature lacked the parameter that
``generate_by_route`` already had). This produced a runtime ``NameError``
the instant any caller invoked the streaming path without supplying
``_log_meta``.

Fix: add ``task_type: str = "by_route_stream"`` to the method signature
(symmetric to ``generate_by_route``) and let the ``_log_meta`` branch fall
back to that parameter.

This test calls ``stream_by_route`` against a fake provider with no
``_log_meta`` and asserts that the chunks are emitted without a
``NameError``.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.services.model_router import (
    BaseProvider,
    GenerationResult,
    ModelRouter,
    TokenUsage,
)
from app.services.prompt_registry import RouteSpec


class _StreamProvider(BaseProvider):
    name = "stream_capture"

    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, Any] | None = None

    async def generate(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        return GenerationResult(
            text="ok",
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model=model,
            provider=self.name,
        )

    async def generate_stream(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.calls += 1
        self.last_kwargs = dict(kw)
        for ch in ("H", "i", "!"):
            yield ch


def _make_router_with_route() -> tuple[ModelRouter, _StreamProvider, RouteSpec]:
    prov = _StreamProvider()
    router = ModelRouter()
    endpoint_uuid = uuid.uuid4()
    ep_key = str(endpoint_uuid)
    router.providers[ep_key] = prov
    router._endpoint_defaults[ep_key] = "test-model-stream"
    router._endpoint_tiers[ep_key] = "standard"
    route = RouteSpec(
        prompt_id=None,
        endpoint_id=endpoint_uuid,
        model="test-model-stream",
        temperature=0.5,
        max_tokens=128,
        system_prompt="",
        mode="text",
    )
    return router, prov, route


def test_stream_by_route_no_log_meta_no_nameerror():
    """Regression: stream_by_route without _log_meta must yield chunks (no NameError)."""
    router, prov, route = _make_router_with_route()

    async def _run() -> list[str]:
        chunks: list[str] = []
        async for ch in router.stream_by_route(
            route=route,
            messages=[{"role": "user", "content": "ping"}],
            # _log_meta deliberately omitted -- the path that used to NameError.
        ):
            chunks.append(ch)
        return chunks

    chunks = asyncio.run(_run())
    assert chunks == ["H", "i", "!"]
    assert prov.calls == 1
    assert prov.last_kwargs is not None
    assert prov.last_kwargs.get("task_type") == "by_route_stream"


def test_stream_by_route_no_log_meta_explicit_task_type_propagates():
    """When caller passes task_type explicitly, provider must see that value."""
    router, prov, route = _make_router_with_route()

    async def _run() -> list[str]:
        chunks: list[str] = []
        async for ch in router.stream_by_route(
            route=route,
            messages=[{"role": "user", "content": "ping"}],
            task_type="polishing",
        ):
            chunks.append(ch)
        return chunks

    chunks = asyncio.run(_run())
    assert chunks == ["H", "i", "!"]
    assert prov.last_kwargs is not None
    assert prov.last_kwargs.get("task_type") == "polishing"

