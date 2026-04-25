"""Celery application configuration."""

import time

from celery import Celery
from celery.signals import (
    task_failure,
    task_prerun,
    task_postrun,
    task_retry,
    task_revoked,
)

from app.config import settings
from app.observability.metrics import CELERY_TASK_DURATION, CELERY_TASK_TOTAL
from app.observability.sentry_init import init_sentry

# Initialize Sentry in the celery process as early as possible (no-op if DSN unset).
init_sentry(component="celery")

celery_app = Celery(
    "ai_write",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

# Celery Beat periodic task schedule
celery_app.conf.beat_schedule = {
    "style-clustering-hourly": {
        "task": "tasks.run_style_clustering",
        "schedule": 3600.0,  # every hour
    },
    "daily-backup": {
        "task": "tasks.run_daily_backup",
        "schedule": 86400.0,  # every 24h; first run ~24h after worker start
    },
}

# Explicitly import task modules so Celery registers them.
import app.tasks.knowledge_tasks  # noqa: F401, E402
import app.tasks.style_tasks  # noqa: F401, E402
import app.tasks.backup_tasks  # noqa: F401, E402


# ---------------------------------------------------------------------------
# Prometheus instrumentation via celery signals.
# task_prerun stamps a monotonic start; task_postrun records duration; status
# is filled by either task_postrun (success), task_failure, task_retry, or
# task_revoked (whichever fires first wins per-task-instance).
# ---------------------------------------------------------------------------
_TASK_START_TIMES: dict[str, float] = {}
_TASK_STATUS: dict[str, str] = {}


def _task_key(task_id: object) -> str:
    return str(task_id) if task_id is not None else "unknown"


@task_prerun.connect
def _on_task_prerun(task_id=None, task=None, **_kwargs):  # type: ignore[no-untyped-def]
    _TASK_START_TIMES[_task_key(task_id)] = time.monotonic()


@task_postrun.connect
def _on_task_postrun(task_id=None, task=None, state=None, **_kwargs):  # type: ignore[no-untyped-def]
    key = _task_key(task_id)
    start = _TASK_START_TIMES.pop(key, None)
    name = getattr(task, "name", None) or "unknown"
    status = _TASK_STATUS.pop(key, None)
    if status is None:
        s = (state or "SUCCESS").upper()
        status = "success" if s == "SUCCESS" else s.lower()
    elapsed = (time.monotonic() - start) if start is not None else 0.0
    CELERY_TASK_DURATION.labels(name, status).observe(elapsed)
    CELERY_TASK_TOTAL.labels(name, status).inc()


@task_failure.connect
def _on_task_failure(task_id=None, **_kwargs):  # type: ignore[no-untyped-def]
    _TASK_STATUS[_task_key(task_id)] = "failure"


@task_retry.connect
def _on_task_retry(request=None, **_kwargs):  # type: ignore[no-untyped-def]
    tid = getattr(request, "id", None)
    _TASK_STATUS[_task_key(tid)] = "retry"


@task_revoked.connect
def _on_task_revoked(request=None, **_kwargs):  # type: ignore[no-untyped-def]
    tid = getattr(request, "id", None)
    _TASK_STATUS[_task_key(tid)] = "revoked"


# v0.5 — RAG rebuild task
@celery_app.task(name="rebuild_rag_for_project", bind=True, max_retries=3)
def rebuild_rag_for_project(self, project_id: str, force: bool = False):
    import asyncio

    from app.services.rag_rebuild import rebuild_rag_for_project_async

    return asyncio.run(
        rebuild_rag_for_project_async(project_id=project_id, force=force)
    )


# v0.6 — Reference book decompile task
@celery_app.task(name="reprocess_reference_book", bind=True, max_retries=2)
def reprocess_reference_book(self, book_id: str):
    import asyncio

    from app.services.reference_ingestor import reprocess_reference_book as _run

    # v1.6 (Bug J): use _run_async_safe to dispose loop-bound caches first.
    result = _run_async_safe(_run(book_id=book_id))

    # v1.7: when the run finishes in ``partial`` state (some style/beat cards
    # missing because of transient upstream LLM failures), schedule the first
    # automatic retry wave. The retry task itself will reschedule further
    # waves with backoff up to ``max_auto_retries``.
    try:
        if isinstance(result, dict) and result.get("status") == "partial":
            from app.services.reference_ingestor import (
                compute_retry_delay,
                max_auto_retries,
            )

            if max_auto_retries() > 0:
                delay = compute_retry_delay(1)
                celery_app.send_task(
                    "retry_reference_book_missing_branches",
                    args=[book_id, 1],
                    countdown=delay,
                )
    except Exception:  # pragma: no cover - scheduling must never break ingest
        import logging
        logging.getLogger(__name__).exception(
            "failed to schedule auto-retry for book %s", book_id
        )
    return result


# v1.7 — Retry only the missing style/beat branches for a partial decompile.
@celery_app.task(
    name="retry_reference_book_missing_branches",
    bind=True,
    max_retries=0,
)
def retry_reference_book_missing_branches(self, book_id: str, attempt: int = 1):
    """Re-run style/beat branches for slices whose cards are still missing.

    Reschedules itself with exponential backoff while the book is still in
    ``partial`` state and ``attempt < max_auto_retries``. Idempotent and
    safe to invoke manually from the frontend (see
    ``POST /api/reference-books/{id}/retry-missing``).
    """
    from app.services.reference_ingestor import (
        compute_retry_delay,
        max_auto_retries,
        retry_missing_branches as _retry,
    )

    result = _run_async_safe(_retry(book_id=book_id, attempt=attempt))

    try:
        status = result.get("status") if isinstance(result, dict) else None
        if status == "partial" and int(attempt) < max_auto_retries():
            next_attempt = int(attempt) + 1
            delay = compute_retry_delay(next_attempt)
            celery_app.send_task(
                "retry_reference_book_missing_branches",
                args=[book_id, next_attempt],
                countdown=delay,
            )
    except Exception:  # pragma: no cover
        import logging
        logging.getLogger(__name__).exception(
            "failed to schedule next retry wave for book %s", book_id
        )
    return result


# v1.6 fix (Bug J): when celery worker reuses the cached AsyncEngine across
# tasks, asyncpg pool ends up bound to the previous task's event loop and
# raises ``RuntimeError: Future <...> attached to a different loop`` on the
# very first ``db.get(...)`` of the next task. ``rebuild_rag_for_project``
# and ``reprocess_reference_book`` above use a bare ``asyncio.run`` and hit
# this when called more than once per worker process. Below helper drops
# loop-bound singletons (ModelRouter HTTP client) and disposes the cached
# AsyncEngine before starting a fresh loop.
def _run_async_safe(coro):
    """Run an async coroutine in a celery task with loop-bound caches reset."""
    import asyncio

    try:
        # v1.7: use reset_model_router() so it also clears _router_locks
        # (which previously left module-level asyncio.Lock objects bound
        # to dead event loops, producing 'Lock is bound to a different
        # event loop' on every retry task after the first one).
        from app.services.model_router import reset_model_router
        reset_model_router()
    except Exception:
        pass
    try:
        import app.db.session as _ses
        _disposal_loop = asyncio.new_event_loop()
        try:
            _disposal_loop.run_until_complete(_ses.engine.dispose())
        finally:
            _disposal_loop.close()
    except Exception:
        pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
