"""v1.7.2 Z3: ensure ModelRouter generate paths emit time_llm_call metrics.

Before Z3, only ``app/services/prompt_registry.py:run_prompt`` wrapped its
call to ``router.generate_with_tier_fallback`` with ``time_llm_call``. As a
result, the 5 *other* callers of ``generate_with_tier_fallback`` (and all
callers of ``generate``, ``generate_stream``, ``generate_by_route``)
produced ZERO ``llm_call_total`` / ``llm_call_duration_seconds`` /
``llm_token_total`` samples.

Z3 moves the wrap inside ``ModelRouter.{generate, generate_stream,
generate_by_route, generate_with_tier_fallback}`` and removes the redundant
outer wrap from prompt_registry.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.observability.metrics import (
    LLM_CALL_DURATION,
    LLM_CALL_TOTAL,
    LLM_TOKEN_TOTAL,
    REGISTRY,
)
from app.services.model_router import (
    BaseProvider,
    GenerationResult,
    ModelRouter,
    TaskRouteConfig,
    TokenUsage,
)


class _CountingProvider(BaseProvider):
    """Provider that returns a fixed Result with non-zero token usage."""

    name = "capture"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.calls += 1
        return GenerationResult(
            text="ok",
            usage=TokenUsage(input_tokens=11, output_tokens=7, total_tokens=18),
            model=model,
            provider=self.name,
        )

    async def generate_stream(self, messages, model, temperature=0.7, max_tokens=4096, **kw):  # type: ignore[override]
        self.calls += 1
        for ch in ("o", "k"):
            yield ch


def _build_router() -> tuple[ModelRouter, _CountingProvider]:
    cap = _CountingProvider()
    router = ModelRouter()
    router.providers["capture"] = cap
    router._endpoint_defaults["capture"] = "test-model"
    router._endpoint_tiers["capture"] = "standard"
    router.task_routing["z3_test_task"] = TaskRouteConfig(
        provider_key="capture", model_name="test-model",
        temperature=0.5, max_tokens=128,
    )
    return router, cap


def _counter_value(name: str, labels: dict[str, str]) -> float:
    """Read a Prometheus counter sample by metric name + labelset.

    Counters expose ``<name>_total`` and ``<name>_created`` series; we want
    the ``_total`` one. ``llm_call_total`` is already the metric name (it
    ends with ``_total``), so the sample series name stays the same.
    """
    val = REGISTRY.get_sample_value(name, labels)
    return float(val) if val is not None else 0.0


def test_generate_emits_llm_call_total_with_real_task_type() -> None:
    router, cap = _build_router()
    provider_label = "_CountingProvider"

    before_total = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )
    before_in = _counter_value(
        "llm_token_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "direction": "input"},
    )
    before_out = _counter_value(
        "llm_token_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "direction": "output"},
    )

    asyncio.run(router.generate(
        task_type="z3_test_task",
        messages=[{"role": "user", "content": "hi"}],
    ))

    after_total = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )
    after_in = _counter_value(
        "llm_token_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "direction": "input"},
    )
    after_out = _counter_value(
        "llm_token_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "direction": "output"},
    )

    assert after_total - before_total == 1.0
    assert after_in - before_in == 11.0
    assert after_out - before_out == 7.0
    assert cap.calls == 1


def test_generate_by_route_emits_llm_call_total_with_default_by_route() -> None:
    router, cap = _build_router()
    provider_label = "_CountingProvider"

    # Synthesize a route-like object compatible with generate_by_route.
    class _R:
        endpoint_id = "capture"
        model = "test-model"
        temperature = 0.4
        max_tokens = 256

    before = _counter_value(
        "llm_call_total",
        {"task_type": "by_route", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )
    asyncio.run(router.generate_by_route(
        _R(), messages=[{"role": "user", "content": "hi"}],
    ))
    after = _counter_value(
        "llm_call_total",
        {"task_type": "by_route", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )
    assert after - before == 1.0
    assert cap.calls == 1


def test_generate_stream_emits_llm_call_total() -> None:
    router, cap = _build_router()
    provider_label = "_CountingProvider"

    before = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )

    async def _drain() -> None:
        async for _ in router.generate_stream(
            task_type="z3_test_task",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

    asyncio.run(_drain())

    after = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "ok"},
    )
    assert after - before == 1.0
    assert cap.calls == 1


def test_generate_records_error_status_on_provider_exception() -> None:
    """Failed provider call must record status='error' (not silently dropped)."""
    router, cap = _build_router()

    class _BoomProvider(BaseProvider):
        name = "capture"

        async def generate(self, messages, model, **kw):  # type: ignore[override]
            raise RuntimeError("upstream boom")

        async def generate_stream(self, messages, model, **kw):  # type: ignore[override]
            raise RuntimeError("unused")
            yield

    boom = _BoomProvider()
    router.providers["capture"] = boom
    provider_label = "_BoomProvider"

    before = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "error"},
    )
    try:
        asyncio.run(router.generate(
            task_type="z3_test_task",
            messages=[{"role": "user", "content": "hi"}],
        ))
        raised = False
    except RuntimeError:
        raised = True

    after = _counter_value(
        "llm_call_total",
        {"task_type": "z3_test_task", "provider": provider_label,
         "model": "test-model", "status": "error"},
    )
    assert raised, "router.generate should re-raise provider errors"
    assert after - before == 1.0
