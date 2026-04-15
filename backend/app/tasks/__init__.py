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

# Auto-discover task modules inside the tasks package.
celery_app.autodiscover_tasks(["app.tasks"])
