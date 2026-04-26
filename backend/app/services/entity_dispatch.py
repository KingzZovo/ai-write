"""Entity extraction dispatch helper (B2', v1.5.0).

All chapter persistence sites (single-chapter API, cascade regenerator,
batch generator post-hook, version rollback, variant promotion, manual
edit, pipeline run) call ``dispatch_entity_extraction`` after the chapter
content_text is written. The helper enqueues a Celery task that owns the
actual Neo4j writes via ``EntityTimelineService.extract_and_update``.

Why a helper instead of inlining ``celery_app.send_task`` everywhere?
- Single import point for all 7+ persistence sites.
- Single place to attach idempotency / metrics / Sentry breadcrumbs.
- Survives Celery being unconfigured in tests (logs + no-ops instead of
  raising) so unit tests for chapter save paths do not need a broker.
- Graceful skip when project_id / chapter_idx is unknown.

Non-blocking by design: any dispatch failure is swallowed with a WARNING
log so user-facing chapter save never fails because the graph extension
is temporarily down.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Public Celery task name. Defined here so call sites do not have to import
# the task module (which would pull celery into sync code paths needlessly).
ENTITY_EXTRACTION_TASK = "entities.extract_chapter"


def dispatch_entity_extraction(
    project_id: str | UUID | None,
    chapter_idx: int | None,
    *,
    caller: str,
    chapter_id: str | UUID | None = None,
    countdown: int = 0,
) -> bool:
    """Enqueue the entity-extraction Celery task for a chapter.

    Returns True if the task was successfully enqueued, False otherwise.
    Failures (broker unreachable, missing IDs, celery import error) are
    logged but never raised -- callers run on the user-facing chapter save
    path and must not fail because of a graph-extension hiccup.
    """
    if project_id is None or chapter_idx is None:
        logger.debug(
            "dispatch_entity_extraction skipped: missing ids "
            "(caller=%s project_id=%s chapter_idx=%s)",
            caller, project_id, chapter_idx,
        )
        return False

    try:
        idx_int = int(chapter_idx)
    except (TypeError, ValueError):
        logger.warning(
            "dispatch_entity_extraction skipped: non-integer chapter_idx=%r (caller=%s)",
            chapter_idx, caller,
        )
        return False

    payload: dict[str, Any] = {
        "project_id": str(project_id),
        "chapter_idx": idx_int,
        "caller": caller,
    }
    if chapter_id is not None:
        payload["chapter_id"] = str(chapter_id)

    try:
        # Local import keeps the celery dependency out of any module that
        # only needs the helper signature (e.g. tests, linters).
        from app.tasks import celery_app
    except Exception as e:  # pragma: no cover - import failure is exceptional
        logger.warning(
            "dispatch_entity_extraction: celery_app unavailable (caller=%s): %s",
            caller, e,
        )
        return False

    try:
        celery_app.send_task(
            ENTITY_EXTRACTION_TASK,
            kwargs=payload,
            countdown=countdown,
        )
    except Exception as e:
        logger.warning(
            "dispatch_entity_extraction: send_task failed "
            "(caller=%s project_id=%s chapter_idx=%d): %s",
            caller, payload["project_id"], idx_int, e,
        )
        return False

    logger.info(
        "Entity extraction enqueued (caller=%s project_id=%s chapter_idx=%d)",
        caller, payload["project_id"], idx_int,
    )
    return True


async def dispatch_for_chapter(
    chapter: Any,
    db: Any,
    *,
    caller: str,
    project_id_hint: str | UUID | None = None,
) -> bool:
    """Resolve (project_id, chapter_idx) from a Chapter ORM object and dispatch.

    Used by every chapter-content persistence site (PATCH endpoint, single
    chapter generate auto-save, variant promotion, version rollback,
    cascade regenerator, batch generator pipeline). Always non-blocking;
    returns True iff the task was enqueued.

    ``project_id_hint`` short-circuits the Volume lookup when the caller
    already has the project id in scope (FastAPI path params, etc.).
    """
    if chapter is None:
        return False
    chapter_idx = getattr(chapter, "chapter_idx", None)
    if chapter_idx is None:
        return False

    project_id: str | UUID | None = project_id_hint
    if project_id is None:
        try:
            from app.models.project import Volume
            volume_id = getattr(chapter, "volume_id", None)
            if volume_id is None:
                return False
            volume = await db.get(Volume, str(volume_id))
            if volume is None:
                return False
            project_id = volume.project_id
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "dispatch_for_chapter: failed to resolve project_id (caller=%s): %s",
                caller, e,
            )
            return False

    return dispatch_entity_extraction(
        project_id=project_id,
        chapter_idx=chapter_idx,
        chapter_id=getattr(chapter, "id", None),
        caller=caller,
    )
