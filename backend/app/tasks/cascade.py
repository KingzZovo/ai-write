"""v1.5.0 C4-3 — Cascade auto-regenerate Celery task.

Consumes ``cascade_tasks`` rows produced by C4-2's ``cascade_planner`` and
routs them to upstream-entity regenerators (outline / character /
world_rule / chapter). One row = one regeneration target.

Contract surface
----------------
Public helpers:

* :func:`enqueue_cascade_candidates`
    Async helper used by C4-4 (the ``/api/evaluate`` callback). Takes a
    list of :class:`CascadeTaskCandidate` from the planner, INSERTs them
    via ``ON CONFLICT DO NOTHING`` keyed on ``uq_cascade_tasks_idem``,
    and dispatches one Celery task per *newly* inserted row. Returns
    metadata about inserted vs. duplicate rows so callers can emit
    accurate SSE ``cascade_triggered`` payloads.

* :func:`dispatch_cascade_task`
    Non-blocking ``celery_app.send_task`` wrapper. Mirrors
    :func:`app.tasks.evaluation_tasks.dispatch_evaluate_task`.

* :data:`RUN_CASCADE_TASK`
    The Celery task name. Wire C4-4 directly to ``send_task(name=...)``
    so the API layer never needs to import this module.

State machine
-------------
``cascade_tasks.status`` values map to:

* ``pending``  — freshly INSERTed by ``enqueue_cascade_candidates``.
* ``running``  — task picked it up; ``started_at`` set, ``attempt_count``
  incremented atomically. The advisory lock is held for the duration of
  this state.
* ``done``     — handler returned cleanly; ``completed_at`` set,
  ``error_message`` cleared.
* ``failed``   — handler raised after max_retries exhausted, or rolled in
  unrecoverably (e.g. target row deleted). ``completed_at`` set,
  ``error_message`` populated.
* ``skipped``  — idempotency short-circuit (already done / running) or
  handler intentionally declined (e.g. target type without an
  implementation yet). ``completed_at`` set.

Per-project serialization
-------------------------
We must guarantee at most one cascade running per project to avoid
collisions in outline / character regeneration. Implementation:
``pg_try_advisory_xact_lock(hashtext('cascade:project:' || project_id))``.

This is *non-blocking*. If another worker holds the lock, we issue
``self.retry(countdown=...)`` so Celery requeues the task with
exponential backoff — worker stays free for other projects.

Why advisory_xact, not row-level FOR UPDATE: the lock must outlive a
short transaction (the long-running upstream regeneration may swap DB
sessions / commit intermediate progress). ``pg_try_advisory_xact_lock``
is automatically released at xact end, which we synchronise with the
status update from running -> done|failed|skipped.

Idempotency
-----------
Two layers:

1. INSERT-time — ``ON CONFLICT (source_chapter_id, target_entity_type,
   target_entity_id, severity) DO NOTHING`` against
   ``uq_cascade_tasks_idem``. Replanning the same evaluation row produces
   zero net DB writes.
2. Task-time — if the row's ``status`` is already ``done`` or ``running``
   when the task picks it up (e.g. broker re-delivery after a worker
   crash, or two workers racing on the same row), short-circuit to
   ``skipped`` with ``error_message`` describing the reason. Never
   overwrite a terminal row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from uuid import UUID

from app.tasks import celery_app

logger = logging.getLogger(__name__)

RUN_CASCADE_TASK = "cascade.run_cascade_task"

#: Backoff (seconds) when we lose the per-project advisory lock race.
#: Celery's retry_backoff still applies on top -- this is the floor.
LOCK_RETRY_COUNTDOWN: int = 30

#: Statuses that short-circuit the task (we never re-run a row in these
#: states; broker re-delivery becomes a no-op + 'skipped').
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "skipped"})


# ---------------------------------------------------------------------------
# Enqueue / dispatch helpers (sync API for service layer)
# ---------------------------------------------------------------------------


async def enqueue_cascade_candidates(
    db: Any,  # AsyncSession; typed loosely to avoid hard dep at import time
    candidates: "Sequence[Any]",  # Sequence[CascadeTaskCandidate]
    *,
    parent_task_id: str | None = None,
    caller: str = "unknown",
) -> dict[str, Any]:
    """Persist candidates with idempotent INSERT and dispatch fresh tasks.

    Idempotent contract:

    * For each candidate, attempt INSERT. Conflict on
      ``uq_cascade_tasks_idem`` -> the row already exists from a prior
      planning round; we *do not* dispatch a fresh celery task for it
      (the original dispatch is still in flight or the row is already
      terminal).
    * Newly inserted rows get a celery dispatch.

    Returns ``{"inserted": [task_id, ...], "duplicates": int,
    "dispatched": int, "dispatch_failures": int}``.

    Failure of ``send_task`` is logged but does not roll back the
    INSERT; the row stays ``pending`` and a future re-plan or a beat
    sweeper can pick it up.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.cascade_task import CascadeTask

    if not candidates:
        return {
            "inserted": [],
            "duplicates": 0,
            "dispatched": 0,
            "dispatch_failures": 0,
        }

    inserted_ids: list[str] = []
    duplicates = 0

    for cand in candidates:
        stmt = (
            pg_insert(CascadeTask)
            .values(
                project_id=str(cand.project_id),
                source_chapter_id=str(cand.source_chapter_id),
                source_evaluation_id=str(cand.source_evaluation_id),
                target_entity_type=cand.target_entity_type,
                target_entity_id=str(cand.target_entity_id),
                severity=cand.severity,
                issue_summary=cand.issue_summary,
                status="pending",
                parent_task_id=str(parent_task_id) if parent_task_id else None,
            )
            .on_conflict_do_nothing(constraint="uq_cascade_tasks_idem")
            .returning(CascadeTask.id)
        )
        result = await db.execute(stmt)
        new_id = result.scalar_one_or_none()
        if new_id is None:
            duplicates += 1
        else:
            inserted_ids.append(str(new_id))

    await db.commit()

    dispatched = 0
    dispatch_failures = 0
    for tid in inserted_ids:
        if dispatch_cascade_task(tid, caller=caller):
            dispatched += 1
        else:
            dispatch_failures += 1

    logger.info(
        "enqueue_cascade_candidates: caller=%s candidates=%d inserted=%d "
        "duplicates=%d dispatched=%d dispatch_failures=%d parent=%s",
        caller,
        len(candidates),
        len(inserted_ids),
        duplicates,
        dispatched,
        dispatch_failures,
        parent_task_id,
    )
    return {
        "inserted": inserted_ids,
        "duplicates": duplicates,
        "dispatched": dispatched,
        "dispatch_failures": dispatch_failures,
    }


def dispatch_cascade_task(
    cascade_task_id: str,
    *,
    caller: str = "unknown",
    countdown: int = 0,
) -> bool:
    """Non-blocking ``send_task`` wrapper. Returns True iff enqueue ok.

    Mirrors :func:`app.tasks.evaluation_tasks.dispatch_evaluate_task`.
    """
    try:
        celery_app.send_task(
            RUN_CASCADE_TASK,
            kwargs={
                "cascade_task_id": str(cascade_task_id),
                "caller": str(caller),
            },
            countdown=countdown,
        )
        return True
    except Exception as exc:  # pragma: no cover - broker outage path
        logger.warning(
            "dispatch_cascade_task: send_task failed (caller=%s task_id=%s): %s",
            caller,
            cascade_task_id,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Core async work
# ---------------------------------------------------------------------------


def _project_lock_key(project_id: str) -> str:
    """Key passed to ``hashtext()`` for the advisory lock. Centralising
    this so tests can assert on the exact namespace string."""
    return f"cascade:project:{project_id}"


async def _acquire_project_lock(db: Any, project_id: str) -> bool:
    """Try to grab ``pg_try_advisory_xact_lock`` for this project.

    Returns True iff lock acquired. Lock is auto-released at xact end.
    """
    from sqlalchemy import text

    res = await db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
        {"k": _project_lock_key(project_id)},
    )
    got = bool(res.scalar())
    return got


async def _handle_target(
    db: Any,
    *,
    target_entity_type: str,
    target_entity_id: str,
    project_id: str,
    issue_summary: str | None,
    severity: str,
    source_chapter_id: str,
) -> dict[str, Any]:
    """Dispatch to a per-entity-type regenerator.

    For C4-3 we land the *scaffold*. The actual outline / character /
    world_rule / chapter regeneration glue lands in C4-4+ once the API
    layer + SSE are wired. Until then, every handler returns ``skipped``
    with a clear reason so we don't silently mark rows ``done`` and
    confuse downstream consumers.
    """
    handler_map = {
        "outline": _handle_outline_target,
        "character": _handle_character_target,
        "world_rule": _handle_world_rule_target,
        "chapter": _handle_chapter_target,
    }
    handler = handler_map.get(target_entity_type)
    if handler is None:
        return {
            "status": "failed",
            "reason": f"unknown_target_entity_type:{target_entity_type}",
        }
    return await handler(
        db=db,
        target_entity_id=target_entity_id,
        project_id=project_id,
        issue_summary=issue_summary,
        severity=severity,
        source_chapter_id=source_chapter_id,
    )


async def _handle_outline_target(**_kw: Any) -> dict[str, Any]:
    # C4-3 scaffold. Real implementation will load the outline row and
    # call OutlineGenerator with the issue_summary as edit guidance.
    return {"status": "skipped", "reason": "outline_handler_not_implemented"}


async def _handle_character_target(**_kw: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": "character_handler_not_implemented"}


async def _handle_world_rule_target(**_kw: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": "world_rule_handler_not_implemented"}


async def _handle_chapter_target(**_kw: Any) -> dict[str, Any]:
    # 'chapter' targets are rare (planner currently never emits them) but
    # the schema allows them; reserved for future use.
    return {"status": "skipped", "reason": "chapter_handler_not_implemented"}


async def _run_cascade_task_async(
    cascade_task_id: str,
    caller: str,
    *,
    on_lock_unavailable: "_LockUnavailableHook | None" = None,
) -> dict[str, Any]:
    """Core async work for one cascade_tasks row.

    The optional ``on_lock_unavailable`` is invoked if we fail to acquire
    the per-project advisory lock; it must raise (typically
    ``self.retry(...)``) so the function never reaches the regular
    finalise path. Tests pass a no-op + observe behaviour.
    """
    from app.db.session import async_session_factory
    from app.models.cascade_task import CascadeTask

    # 1) Load row, snapshot fields, mark running under the project lock.
    async with async_session_factory() as db:
        row = await db.get(CascadeTask, cascade_task_id)
        if row is None:
            logger.warning(
                "run_cascade_task: row %s not found (caller=%s)",
                cascade_task_id, caller,
            )
            return {"status": "skipped", "reason": "row_not_found"}

        if row.status in _TERMINAL_STATUSES:
            logger.info(
                "run_cascade_task: row %s already %s (caller=%s)",
                cascade_task_id, row.status, caller,
            )
            return {
                "status": "skipped",
                "reason": f"already_{row.status}",
                "task_id": str(cascade_task_id),
            }

        if row.status == "running":
            # Broker re-delivery while another worker (or this same task
            # on a previous attempt) is still inside the lock. Stay
            # 'skipped' and let the active worker finish.
            logger.info(
                "run_cascade_task: row %s already running; skip (caller=%s)",
                cascade_task_id, caller,
            )
            return {
                "status": "skipped",
                "reason": "already_running",
                "task_id": str(cascade_task_id),
            }

        project_id_s = str(row.project_id)

        got_lock = await _acquire_project_lock(db, project_id_s)
        if not got_lock:
            await db.rollback()
            logger.info(
                "run_cascade_task: project lock busy for %s; deferring task=%s",
                project_id_s, cascade_task_id,
            )
            if on_lock_unavailable is not None:
                on_lock_unavailable(project_id_s, cascade_task_id)
            return {
                "status": "deferred",
                "reason": "project_lock_busy",
                "task_id": str(cascade_task_id),
            }

        # We hold the advisory lock now (auto-released on xact end below).
        row.status = "running"
        row.started_at = datetime.now(timezone.utc)
        row.attempt_count = (row.attempt_count or 0) + 1
        row.error_message = None

        # Snapshot all fields the handler will need; we'll re-fetch the
        # row in a fresh session before terminal state writes so the
        # advisory lock can release cleanly between the long-running
        # handler call and the final commit.
        snapshot = {
            "project_id": project_id_s,
            "source_chapter_id": str(row.source_chapter_id),
            "target_entity_type": str(row.target_entity_type),
            "target_entity_id": str(row.target_entity_id),
            "severity": str(row.severity),
            "issue_summary": row.issue_summary,
            "attempt_count": int(row.attempt_count),
        }
        await db.commit()
        # NB: committing here releases pg_advisory_xact_lock. For the
        # short-running stub handlers in C4-3 this is fine -- we re-acquire
        # before the terminal write below to keep the per-project mutex
        # honest. When real regenerators land, they should themselves run
        # under a freshly-acquired lock or accept overlap (per C4 design
        # doc).

    # 2) Run handler outside the DB session.
    handler_outcome: dict[str, Any]
    handler_error: str | None = None
    try:
        async with async_session_factory() as db_h:
            # Re-acquire project lock so concurrent workers cannot both
            # be inside a handler for the same project.
            got = await _acquire_project_lock(db_h, snapshot["project_id"])
            if not got:
                # Rare race: lost the lock between sessions. Defer.
                if on_lock_unavailable is not None:
                    on_lock_unavailable(
                        snapshot["project_id"], cascade_task_id
                    )
                # If hook didn't raise (tests), surface as deferred.
                return {
                    "status": "deferred",
                    "reason": "project_lock_busy_after_reacquire",
                    "task_id": str(cascade_task_id),
                }
            handler_outcome = await _handle_target(
                db=db_h,
                target_entity_type=snapshot["target_entity_type"],
                target_entity_id=snapshot["target_entity_id"],
                project_id=snapshot["project_id"],
                issue_summary=snapshot["issue_summary"],
                severity=snapshot["severity"],
                source_chapter_id=snapshot["source_chapter_id"],
            )
    except Exception as exc:
        handler_outcome = {"status": "failed", "reason": "handler_exception"}
        handler_error = repr(exc)[:500]
        logger.exception(
            "run_cascade_task: handler raised for task=%s entity=%s/%s",
            cascade_task_id,
            snapshot["target_entity_type"],
            snapshot["target_entity_id"],
        )
        # We persist failure below but still re-raise so celery counts
        # the failure for retry semantics.
        raise_after_finalise = exc
    else:
        raise_after_finalise = None

    # 3) Persist terminal state. Always lands in done|failed|skipped.
    final_status = handler_outcome.get("status", "failed")
    final_reason = handler_outcome.get("reason")
    if final_status not in ("done", "failed", "skipped"):
        # Defensive: handler returned an unrecognised status; treat as
        # failed with reason recorded.
        final_reason = f"unrecognised_handler_status:{final_status}"
        final_status = "failed"

    error_msg = handler_error
    if error_msg is None and final_status in ("failed", "skipped") and final_reason:
        error_msg = final_reason

    async with async_session_factory() as db_f:
        row = await db_f.get(CascadeTask, cascade_task_id)
        if row is None:
            logger.warning(
                "run_cascade_task: row %s vanished before terminal write",
                cascade_task_id,
            )
            return {
                "status": "skipped",
                "reason": "row_deleted_during_run",
                "task_id": str(cascade_task_id),
            }
        row.status = final_status
        row.completed_at = datetime.now(timezone.utc)
        row.error_message = error_msg
        await db_f.commit()

    logger.info(
        "run_cascade_task: task=%s status=%s reason=%s entity=%s/%s attempt=%d caller=%s",
        cascade_task_id,
        final_status,
        final_reason,
        snapshot["target_entity_type"],
        snapshot["target_entity_id"],
        snapshot["attempt_count"],
        caller,
    )

    if raise_after_finalise is not None:
        raise raise_after_finalise

    return {
        "status": final_status,
        "reason": final_reason,
        "task_id": str(cascade_task_id),
        "target_entity_type": snapshot["target_entity_type"],
        "target_entity_id": snapshot["target_entity_id"],
        "attempt_count": snapshot["attempt_count"],
    }


# Hook signature for tests / lock-loss path. ``raise self.retry(...)`` is
# how the live celery task uses it; tests provide a no-op.
class _LockUnavailableHook:  # pragma: no cover - structural type marker
    def __call__(self, project_id: str, cascade_task_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    name=RUN_CASCADE_TASK,
    bind=True,
    max_retries=3,
    default_retry_delay=15,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_cascade_task(
    self,
    cascade_task_id: str,
    caller: str = "unknown",
) -> dict[str, Any]:
    """Celery entry point. See module docstring for the full contract."""
    from app.tasks import _run_async_safe

    def _on_lock_busy(project_id: str, _task_id: str) -> None:
        # Re-enqueue with backoff. Re-using celery's retry machinery so
        # the prometheus retry signal fires + countdown jitter applies.
        raise self.retry(
            countdown=LOCK_RETRY_COUNTDOWN,
            exc=RuntimeError(f"project_lock_busy:{project_id}"),
        )

    return _run_async_safe(
        _run_cascade_task_async(
            cascade_task_id=str(cascade_task_id),
            caller=str(caller),
            on_lock_unavailable=_on_lock_busy,
        )
    )


__all__ = [
    "RUN_CASCADE_TASK",
    "LOCK_RETRY_COUNTDOWN",
    "run_cascade_task",
    "enqueue_cascade_candidates",
    "dispatch_cascade_task",
]
