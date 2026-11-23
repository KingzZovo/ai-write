"""Celery application configuration."""

import logging
import time

logger = logging.getLogger(__name__)

# 关事件循环前,给任务内部 create_task 发射的后台任务(自动压缩等)留的排空上限。
_DETACHED_TASK_DRAIN_SECONDS = 120

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
    task_routes={
        "tasks.dispatch_pipeline_commands": {"queue": "pipeline-control"},
        "tasks.dispatch_generation_batches": {"queue": "pipeline-control"},
        "tasks.dispatch_generation_tasks": {"queue": "pipeline-control"},
        "tasks.dispatch_arc_planning": {"queue": "pipeline-control"},
        # 认知提取走独立队列。
        # 原因(实测 2026-11-22):它原本与写作共用 concurrency=1 的 worker,写作
        # 任务排队时认知提取一等十几分钟;而 pipeline 推进下一章要求前一章的认知
        # 记录为 completed(边界全有或全无),于是 ch13 pending 的 12 分钟里,
        # 后面的 73 章全部失败。独立队列 + 独立 worker 让两者并行、互不阻塞。
        "cognition.extract_chapter": {"queue": "cognition"},
    },
)

# v1.10 fix (broker visibility_timeout): default Redis broker visibility is
# 1h, but reference-book retry tasks routinely run 60-90s and decompile tasks
# can run for many minutes. When a task exceeds the visibility_timeout, the
# broker re-delivers the same message to another (or the same) worker,
# producing duplicate runs of the same retry attempt. Bump to 2h so single
# tasks never get re-delivered while still allowing dead-worker reclaim.
celery_app.conf.broker_transport_options = {
    "visibility_timeout": 7200,  # 2 hours, in seconds
}

# Celery Beat periodic task schedule
celery_app.conf.beat_schedule = {
    "arc-planning-recovery": {"task": "tasks.dispatch_arc_planning", "schedule": 30.0,
                              "options": {"expires": 30}},
    "independent-generation-recovery": {"task": "tasks.dispatch_generation_tasks", "schedule": 30.0,
                                        "options": {"expires": 30}},
    "generation-batch-recovery": {"task": "tasks.dispatch_generation_batches", "schedule": 30.0,
                                  "options": {"expires": 30}},
    "relationship-write-recovery": {"task": "entities.replay_relationship_writes", "schedule": 60.0, "options": {"expires": 60}},
    "foreshadow-write-recovery": {"task": "foreshadows.replay_writes", "schedule": 60.0, "options": {"expires": 60}},
    "foreshadow-confirmation-recovery": {"task": "foreshadows.replay_confirmations", "schedule": 60.0, "options": {"expires": 60}},
    "foreshadow-review-recovery": {"task": "foreshadows.replay_reviews", "schedule": 60.0, "options": {"expires": 60}},
    "evaluation-recovery": {"task": "evaluations.recover_pending", "schedule": 60.0, "options": {"expires": 60}},
    "character-deletion-recovery": {"task": "entities.replay_character_deletions", "schedule": 60.0, "options": {"expires": 60}},
    "author-profile-recovery": {"task": "entities.replay_author_profiles", "schedule": 60.0, "options": {"expires": 60}},
    # 2026-11-17 审计:此前这些条目没有 ``expires``,单轮跑超一个周期时
    # broker 会再投一次 → 同类恢复任务叠加运行(style-clustering-hourly
    # 每小时全量重抽取可把 CPU 打满)。统一给每个周期任务加 expires。
    "outline-settings-recovery": {"task": "entities.replay_outline_settings", "schedule": 60.0,
                                  "options": {"expires": 60}},
    "entity-source-invalidations": {"task": "entities.replay_invalidations", "schedule": 30.0,
                                    "options": {"expires": 30}},
    "entity-source-outbox": {"task": "entities.drain_source_outbox", "schedule": 30.0,
                             "options": {"expires": 30}},
    "cognition-extraction-recovery": {"task": "cognition.replay_pending", "schedule": 60.0,
                                      "options": {"expires": 60}},
    "cognition-index-cleanup": {"task": "cognition.cleanup_deleted_projects", "schedule": 3600.0,
                                "options": {"expires": 3600}},
    "entity-projection-recovery": {"task": "entities.replay_pending", "schedule": 60.0,
                                   "options": {"expires": 60}},
    "pipeline-command-recovery": {
        "task": "tasks.dispatch_pipeline_commands",
        "schedule": 30.0,
        "options": {"expires": 30},
    },
    "style-clustering-hourly": {
        "task": "tasks.run_style_clustering",
        "schedule": 3600.0,  # every hour
        "options": {"expires": 3600},
    },
    "daily-backup": {
        "task": "tasks.run_daily_backup",
        "schedule": 86400.0,  # every 24h; first run ~24h after worker start
        "options": {"expires": 86400},
    },
    # v1.11 scaling groundwork (migration a1001916/a1001917): keep future
    # llm_call_logs monthly partitions pre-created and apply retention.
    # No-ops with a "skipped" result until the partition migration runs.
    "llm-log-partition-maintenance-daily": {
        "task": "tasks.maintain_llm_log_partitions",
        "schedule": 86400.0,
        "options": {"expires": 86400},
    },
    # Keep-last-K chapter version pruning; default (K=0) keeps everything.
    "chapter-version-retention-daily": {
        "task": "tasks.enforce_chapter_version_retention",
        "schedule": 86400.0,
        "options": {"expires": 86400},
    },
}

# Explicitly import task modules so Celery registers them.
import app.tasks.generation_tasks  # noqa: F401, E402  # split from knowledge_tasks (2026-07-26)
import app.tasks.arc_tasks  # noqa: F401, E402
import app.tasks.book_tasks  # noqa: F401, E402
import app.tasks.analysis_tasks  # noqa: F401, E402
import app.tasks.knowledge_tasks  # noqa: F401, E402  # compat facade (re-exports)
import app.tasks.style_tasks  # noqa: F401, E402
import app.tasks.backup_tasks  # noqa: F401, E402
import app.tasks.foreshadow_tasks  # noqa: F401, E402
import app.tasks.entity_tasks  # noqa: F401, E402  # B2' (v1.5.0): entity extraction
import app.tasks.evaluation_tasks  # noqa: F401, E402  # C2 Step D (v1.5.0): async chapter eval
import app.tasks.cascade  # noqa: F401, E402  # C4-3 (v1.5.0): cascade auto-regenerate
import app.tasks.retention_tasks  # noqa: F401, E402  # v1.11: partition upkeep + retention


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
    pass

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
    # v1.15: single-flight by book. When a retry wave overruns Redis broker
    # visibility_timeout, Redis can redeliver the same logical wave while the
    # original process is still running. Prevent duplicate same-book waves from
    # piling up and consuming all worker slots. The TTL is intentionally longer
    # than visibility_timeout; if the worker dies, Redis releases it eventually.
    lock_key = f"decompile_retry:lock:{book_id}"
    lock_token = str(getattr(getattr(self, "request", None), "id", None) or "unknown")
    lock_client = None
    lock_acquired = False
    try:
        import os as _os
        import redis

        lock_ttl = int(_os.getenv("DECOMPILE_RETRY_LOCK_TTL", "10800"))
        lock_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        lock_acquired = bool(lock_client.set(lock_key, lock_token, nx=True, ex=lock_ttl))
    except Exception:
        # Redis lock failure must not block the retry mechanism; fall back to
        # normal execution and let task idempotency protect rows.
        lock_client = None
        lock_acquired = True

    if not lock_acquired:
        return {
            "status": "partial",
            "style_filled": 0,
            "beat_filled": 0,
            "locked": True,
            "lock_key": lock_key,
        }

    from app.services.reference_ingestor import (
        compute_retry_delay,
        max_auto_retries,
        retry_missing_branches as _retry,
    )

    try:
        result = _run_async_safe(_retry(book_id=book_id, attempt=attempt))
    finally:
        if lock_client is not None and lock_acquired:
            try:
                if lock_client.get(lock_key) == lock_token:
                    lock_client.delete(lock_key)
                lock_client.close()
            except Exception:
                pass

    try:
        status = result.get("status") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("locked"):
            return result
        # v1.14: progress-aware rescheduling. The legacy contract used a
        # bounded exponential backoff (300s, 600s, 1200s, ... capped at
        # ``max_auto_retries`` waves) which is correct when nothing is
        # happening but counterproductive when the wave is actually
        # draining slices steadily. New behaviour:
        #   * If this wave filled >0 cards AND the book is still partial,
        #     fire the next wave on a SHORT fixed delay (default 30s) and
        #     RESET ``attempt`` to 1 so the backoff clock starts fresh next
        #     time we actually stall. This lets a 20k-slice book drain
        #     batch-by-batch without artificial pauses.
        #   * If this wave filled 0 cards (true stall) and the book is
        #     still partial, retry on a short fixed delay as well. The
        #     ``attempt`` counter is kept only as observable metadata and is
        #     wrapped back to 1 after ``max_auto_retries`` instead of stopping:
        #     upstream LLM outages can last for hours, but the missing-card
        #     backfill must keep probing until the book reaches ready.
        if status == "partial":
            style_filled = int(result.get("style_filled", 0) or 0)
            beat_filled = int(result.get("beat_filled", 0) or 0)
            made_progress = (style_filled + beat_filled) > 0
            if made_progress:
                # Short fixed delay; reset attempt so backoff restarts only
                # when progress later stops.
                import os as _os
                fast_delay = int(_os.getenv("DECOMPILE_RETRY_FAST_DELAY", "30"))
                celery_app.send_task(
                    "retry_reference_book_missing_branches",
                    args=[book_id, 1],
                    countdown=fast_delay,
                )
            else:
                max_attempts = max(1, int(max_auto_retries() or 1))
                next_attempt = int(attempt) + 1 if int(attempt) < max_attempts else 1
                # Upstream LLM outages are usually transient/intermittent.
                # If a wave fills 0 cards, retry soon and do not stop at the
                # historical attempt cap; the single-flight lock prevents
                # duplicate same-book waves from piling up.
                import os as _os
                stall_delay = int(_os.getenv("DECOMPILE_RETRY_STALL_DELAY", "60"))
                celery_app.send_task(
                    "retry_reference_book_missing_branches",
                    args=[book_id, next_attempt],
                    countdown=stall_delay,
                )
    except Exception:  # pragma: no cover
        import logging
        logging.getLogger(__name__).exception(
            "failed to schedule next retry wave for book %s", book_id
        )
    return result


# v1.13 fix (cross-loop crash on the SECOND celery task in a worker):
#
# Bug J / v1.10 only rewrote ``_ses.engine`` and ``_ses.async_session_factory``
# on the db.session module between tasks. But 21 call sites across the
# codebase had already done ``from app.db.session import async_session_factory``
# at import time, copying the original sessionmaker reference. Those callers
# kept using the original sessionmaker → the original asyncpg pool → connections
# bound to the previous task's (now-closed) event loop →
# ``RuntimeError: Future <...> attached to a different loop`` on the very
# first ``db.get(...)`` of every subsequent task.
#
# v1.13: db.session now exposes ``async_session_factory`` as a *function*
# that always reads the current sessionmaker via a small mutable state
# holder. We just call ``reset_engine()`` between tasks and dispose the
# old pool on the right loop in the finally block; all 21 callers pick up
# the fresh pool automatically without any import changes.
def _run_async_safe(coro):
    """Run an async coroutine in a celery task with loop-bound caches reset."""
    import asyncio

    # Clear loop-bound module singletons in model_router (asyncio.Lock
    # objects under ``_router_locks``, etc.). Without this, every second
    # task hit ``Lock is bound to a different event loop``.
    try:
        from app.services.model_router import reset_model_router
        reset_model_router()
    except Exception:
        pass

    # Drop the cached AsyncEngine. Next call to async_session_factory()
    # will lazily build a fresh engine bound to the new loop.
    try:
        from app.db.session import reset_engine
        reset_engine()
    except Exception:
        pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # Dispose any engine that was lazily built during the task on
        # this same loop, so asyncpg connections are closed by the loop
        # that created them. Safe no-op if no engine was built.
        try:
            from app.db.session import dispose_current_engine_async
            loop.run_until_complete(dispose_current_engine_async())
        except Exception:
            pass
        # 2026-11-17 审计:此前只等 ``coro`` 就关循环。任务内部用
        # create_task 发射的"后台小任务"(章摘要后的自动压缩等)会被直接
        # 丢弃 —— 压缩做到一半,compacted 标记与 rollup 长期不一致。
        # 关循环前把剩余任务排空,带上限避免拖住 worker。
        try:
            _pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if _pending:
                logger.info(
                    "draining %d detached task(s) before closing loop", len(_pending)
                )
                loop.run_until_complete(
                    asyncio.wait(_pending, timeout=_DETACHED_TASK_DRAIN_SECONDS)
                )
        except Exception:  # noqa: BLE001 - 排空失败不能让任务本身失败
            logger.warning("failed to drain detached tasks", exc_info=True)
        # Close the loop's Neo4j driver before the loop dies. Drivers are bound
        # to the loop that created them and this teardown never used to call
        # close_neo4j(), so every task leaked a driver into app.db.neo4j._drivers
        # (and a closed loop's cached driver could resurface). Safe no-op when
        # the task never touched Neo4j.
        try:
            from app.db.neo4j import close_neo4j
            loop.run_until_complete(close_neo4j())
        except Exception:  # noqa: BLE001 - closing a driver must not fail the task
            pass
        loop.close()
        loop.close()
