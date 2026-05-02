"""Prometheus metrics singletons.

All counters / histograms are module-level so callers just
`from app.observability.metrics import LLM_CALL_TOTAL, time_llm_call`
without worrying about double-registration.

Exposed on `GET /metrics` (public, intranet only). Scraped by the
`prometheus` service in docker-compose.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# Use a dedicated registry so we do not pollute the default one with per-request
# process metrics if we later decide to split scrapes. Default "process" /
# "platform" collectors are registered into the default registry automatically
# if imported from prometheus_client.
REGISTRY = CollectorRegistry(auto_describe=True)

# -------------------- HTTP --------------------
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    labelnames=("method", "path_template", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=REGISTRY,
)

HTTP_REQUEST_TOTAL = Counter(
    "http_request_total",
    "Total HTTP requests by outcome",
    labelnames=("method", "path_template", "status"),
    registry=REGISTRY,
)

# -------------------- LLM --------------------
LLM_CALL_TOTAL = Counter(
    "llm_call_total",
    "Total LLM calls by task / provider / outcome",
    labelnames=("task_type", "provider", "model", "status"),
    registry=REGISTRY,
)

LLM_CALL_DURATION = Histogram(
    "llm_call_duration_seconds",
    "LLM call latency in seconds",
    labelnames=("task_type", "provider", "model"),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
    registry=REGISTRY,
)

LLM_TOKEN_TOTAL = Counter(
    "llm_token_total",
    "Total tokens consumed by direction (input/output)",
    labelnames=("task_type", "provider", "model", "direction"),
    registry=REGISTRY,
)

# -------------------- Generation runs --------------------
GENERATION_RUN_PHASE = Counter(
    "generation_run_phase_total",
    "Generation run phase transitions (plan/recall/draft/critic/rewrite/finalize)",
    labelnames=("phase", "status"),
    registry=REGISTRY,
)


@contextmanager
def time_llm_call(
    task_type: str,
    provider: str,
    model: str,
) -> Iterator[dict]:
    """Context manager that records latency and success/failure for an LLM call.

    Yields a mutable dict the caller can fill with `input_tokens` /
    `output_tokens` (recorded on exit) and `status` (override; default = 'ok'
    unless exception).
    """
    box: dict = {"status": "ok", "input_tokens": 0, "output_tokens": 0}
    start = time.monotonic()
    try:
        yield box
    except BaseException:
        box["status"] = "error"
        raise
    finally:
        elapsed = time.monotonic() - start
        status = box.get("status") or "ok"
        LLM_CALL_DURATION.labels(task_type, provider, model).observe(elapsed)
        LLM_CALL_TOTAL.labels(task_type, provider, model, status).inc()
        in_tok = int(box.get("input_tokens") or 0)
        out_tok = int(box.get("output_tokens") or 0)
        if in_tok:
            LLM_TOKEN_TOTAL.labels(task_type, provider, model, "input").inc(in_tok)
        if out_tok:
            LLM_TOKEN_TOTAL.labels(task_type, provider, model, "output").inc(out_tok)


def render_latest() -> tuple[bytes, str]:
    """Render exposition format for /metrics. Returns (body, content_type)."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
