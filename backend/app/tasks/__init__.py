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

    return asyncio.run(_run(book_id=book_id))
