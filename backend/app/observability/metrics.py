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
    Gauge,
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
    "http_requests_total",
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

LLM_CACHE_TOKEN_TOTAL = Counter(
    "llm_cache_token_total",
    "Cache-related input tokens by kind (cache_create / cache_read / cache_uncached)",
    labelnames=("task_type", "provider", "model", "kind"),
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

# -------------------- v1.6.0 X4: scene_mode -------------------
SCENE_PLAN_FALLBACK_TOTAL = Counter(
    "scene_plan_fallback_total",
    "scene_planner fallback invocations (heuristic briefs used)",
    labelnames=("reason",),  # unparseable | too_few
    registry=REGISTRY,
)

SCENE_COUNT_PER_CHAPTER = Histogram(
    "scene_count_per_chapter",
    "Number of scenes produced per chapter generation",
    buckets=(1, 2, 3, 4, 5, 6, 7, 8, 10, 12),
    registry=REGISTRY,
)

SCENE_REVISE_ROUND_TOTAL = Counter(
    "scene_revise_round_total",
    "Auto-revise round outcomes (per round_idx)",
    labelnames=("outcome",),  # scored | skipped | revised | timeout | error
    registry=REGISTRY,
)

# -------------------- v1.8.1: SSE auto-save persistence -----
# Tracks whether the post-stream `target_chapter.content_text =` /
# `outline = Outline(...)` write actually committed. Before v1.8.1, save
# failures were swallowed by a `logger.warning("Failed to auto-save ...")`
# inside `api/generate.py`, leaving `chapters.content_text` / `outlines` 0
# while SSE clients showed streamed text. This counter makes that gap
# observable; alert on `failure` rate > 0.
CHAPTER_AUTO_SAVE_TOTAL = Counter(
    "chapter_auto_save_total",
    "Outcomes of post-SSE auto-save into chapters/outlines (success|failure)",
    labelnames=("kind", "outcome", "reason"),  # kind=chapter|outline; outcome=success|failure; reason=ok|<ExceptionClass>
    registry=REGISTRY,
)

# -------------------- Celery --------------------
CELERY_TASK_TOTAL = Counter(
    "celery_task_total",
    "Total celery tasks by name and status (success/failure/retry/revoked)",
    labelnames=("task_name", "status"),
    registry=REGISTRY,
)

CELERY_TASK_DURATION = Histogram(
    "celery_task_duration_seconds",
    "Celery task wall-clock latency in seconds",
    labelnames=("task_name", "status"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)

# -------------------- DB connection pool --------------------
# Gauges populated lazily by `_collect_db_pool_gauges()` before each scrape
# (registered via a sidecar collector below). Labels: pool="main".
DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "SQLAlchemy connection pool max size",
    labelnames=("pool",),
    registry=REGISTRY,
)
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "SQLAlchemy connections currently checked out from the pool",
    labelnames=("pool",),
    registry=REGISTRY,
)
DB_POOL_OVERFLOW = Gauge(
    "db_pool_overflow",
    "SQLAlchemy connections beyond pool_size currently in use",
    labelnames=("pool",),
    registry=REGISTRY,
)


def _refresh_db_pool_gauges() -> None:
    """Sample current SQLAlchemy pool stats and write them into gauges.

    Imported lazily so this module does not pull in db.session at import time
    (avoids cycles + lets unit tests import metrics standalone).
    """
    try:
        from app.db.session import engine

        pool = engine.pool
        DB_POOL_SIZE.labels("main").set(float(pool.size()))
        DB_POOL_CHECKED_OUT.labels("main").set(float(pool.checkedout()))
        # overflow() returns -1 when pool has not overflowed; clamp to >=0.
        ovr = pool.overflow()
        DB_POOL_OVERFLOW.labels("main").set(float(max(ovr, 0)))
    except Exception:
        # Never break /metrics on a transient pool sampling error.
        pass


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
    _refresh_db_pool_gauges()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
