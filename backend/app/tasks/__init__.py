"""Celery application configuration."""

from celery import Celery

from app.config import settings

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
}

# Explicitly import task modules so Celery registers them.
import app.tasks.knowledge_tasks  # noqa: F401, E402
import app.tasks.style_tasks  # noqa: F401, E402


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
