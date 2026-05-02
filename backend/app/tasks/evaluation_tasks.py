"""v1.5.0 C2 Step D — async chapter evaluation celery task.

Decouples the 30-90s ChapterEvaluator LLM call from the request thread.
The POST /api/evaluate/start endpoint inserts an EvaluateTask row in
status='pending' and enqueues this task; the task itself flips the row
through running -> completed | failed and persists the EvaluationResult
snapshot under result_json so GET /api/evaluate/tasks/{id} can answer
status polls without re-running the LLM.

Why a separate Celery task (not just BackgroundTasks)?
- Survives backend restarts (BackgroundTasks die with the worker).
- Plays well with the existing celery autoretry / Sentry / Prometheus
  signal handlers (CELERY_TASK_DURATION, etc.).
- Mirrors entity_tasks.py B2' contract: bind=True, autoretry_for=Exception,
  retry_backoff, retry_backoff_max=300, retry_jitter, all wrapped through
  ``_run_async_safe`` for the v1.13 cross-loop hardening.

Idempotency:
- The EvaluateTask row's primary key is owned by the API layer; this task
  takes that id and is the only writer that flips it past 'pending'.
- A retry sees status in {'pending','running','failed'} -> proceeds and
  overwrites result_json. Already-completed rows are short-circuited.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.tasks import celery_app

logger = logging.getLogger(__name__)

EVALUATE_CHAPTER_TASK = "evaluations.evaluate_chapter"


async def _run_evaluate_task_async(
    evaluate_task_id: str,
    chapter_id: str,
    caller: str,
) -> dict[str, Any]:
    """Core async work: load chapter, run evaluator, persist result."""
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import Chapter, EvaluateTask
    from app.services.chapter_evaluator import ChapterEvaluator

    # 1) Load row + chapter; mark running.
    async with async_session_factory() as db:
        row = await db.get(EvaluateTask, evaluate_task_id)
        if row is None:
            logger.warning(
                "evaluate_chapter_task: row %s not found (caller=%s)",
                evaluate_task_id, caller,
            )
            return {"status": "skipped", "reason": "row_not_found"}

        if row.status == "completed":
            logger.info(
                "evaluate_chapter_task: row %s already completed; skip (caller=%s)",
                evaluate_task_id, caller,
            )
            return {
                "status": "skipped",
                "reason": "already_completed",
                "task_id": str(evaluate_task_id),
            }

        chapter = await db.get(Chapter, str(chapter_id))
        if chapter is None or not (chapter.content_text or "").strip():
            row.status = "failed"
            row.error_text = "chapter_missing_or_empty"
            row.completed_at = datetime.now(timezone.utc)
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return {
                "status": "failed",
                "reason": "chapter_missing_or_empty",
                "task_id": str(evaluate_task_id),
            }

        chapter_text = chapter.content_text
        chapter_outline = chapter.outline_json or {}

        row.status = "running"
        row.started_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()

    # 2) Run evaluator OUTSIDE the DB session (LLM call may take 30-90s).
    try:
        evaluator = ChapterEvaluator()
        eval_result = await evaluator.evaluate(
            chapter_text=chapter_text,
            chapter_outline=chapter_outline,
        )
    except Exception as exc:
        # Persist failure status before re-raising so celery retry sees state.
        async with async_session_factory() as db:
            row = await db.get(EvaluateTask, evaluate_task_id)
            if row is not None:
                row.status = "failed"
                row.error_text = repr(exc)[:500]
                row.completed_at = datetime.now(timezone.utc)
                row.updated_at = datetime.now(timezone.utc)
                await db.commit()
        raise

    # 3) Persist completed result.
    async with async_session_factory() as db:
        row = await db.get(EvaluateTask, evaluate_task_id)
        if row is None:
            # Row was deleted while we were running; surface a no-op.
            return {
                "status": "skipped",
                "reason": "row_deleted_during_run",
                "task_id": str(evaluate_task_id),
            }
        row.status = "completed"
        row.result_json = eval_result.to_dict()
        row.completed_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        row.error_text = None
        await db.commit()

    logger.info(
        "evaluate_chapter_task: completed task=%s chapter=%s overall=%.2f issues=%d caller=%s",
        evaluate_task_id, chapter_id, eval_result.overall, len(eval_result.issues), caller,
    )
    return {
        "status": "ok",
        "task_id": str(evaluate_task_id),
        "chapter_id": str(chapter_id),
        "overall": eval_result.overall,
        "issues": len(eval_result.issues),
    }


@celery_app.task(
    name=EVALUATE_CHAPTER_TASK,
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)
def evaluate_chapter_task(
    self,
    evaluate_task_id: str,
    chapter_id: str,
    caller: str = "unknown",
) -> dict[str, Any]:
    """Run ChapterEvaluator for a chapter and persist into evaluate_tasks row.

    See module docstring for the full contract.
    """
    from app.tasks import _run_async_safe

    return _run_async_safe(
        _run_evaluate_task_async(
            evaluate_task_id=str(evaluate_task_id),
            chapter_id=str(chapter_id),
            caller=str(caller),
        )
    )


def dispatch_evaluate_task(
    evaluate_task_id: str,
    chapter_id: str,
    *,
    caller: str,
    countdown: int = 0,
) -> bool:
    """Helper to enqueue evaluate_chapter_task non-blockingly.

    Mirrors ``dispatch_entity_extraction``: never raises, returns True iff
    the task was successfully enqueued. Failures are logged and the API
    layer can fall back to leaving the row in 'pending' (a future poll or
    a celery beat sweeper can re-dispatch).
    """
    try:
        celery_app.send_task(
            EVALUATE_CHAPTER_TASK,
            kwargs={
                "evaluate_task_id": str(evaluate_task_id),
                "chapter_id": str(chapter_id),
                "caller": str(caller),
            },
            countdown=countdown,
        )
    except Exception as e:
        logger.warning(
            "dispatch_evaluate_task: send_task failed (caller=%s task_id=%s): %s",
            caller, evaluate_task_id, e,
        )
        return False
    logger.info(
        "evaluate_chapter_task enqueued (caller=%s task_id=%s chapter=%s)",
        caller, evaluate_task_id, chapter_id,
    )
    return True
