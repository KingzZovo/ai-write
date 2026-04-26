"""Entity extraction celery task (B2', v1.5.0).

Owns the Neo4j Character/CharacterState/RELATES_TO/AT_LOCATION write path.
For each (project_id, chapter_idx) it:

1. Marks the chapter via an ``ExtractionMarker`` node in Neo4j (atomic
   MERGE with status). Skips work if status='completed'.
2. Lazily ensures Neo4j constraints/indexes via
   ``EntityTimelineService.initialize_graph`` (idempotent, no-op once
   constraints exist). This guarantees the read-side query (the source of
   the 49 GqlStatus warnings) sees label/property metadata.
3. Loads the chapter's ``content_text`` from Postgres and runs
   ``EntityTimelineService.extract_and_update`` which performs the
   tier-aware LLM extraction + multi-statement Neo4j writes.
4. On success flips the marker to ``completed``; on failure flips to
   ``failed`` and lets celery retry with exponential backoff.

Designed to:
- Be safe to fire from ALL chapter persistence sites (single-chapter API,
  cascade regenerator, batch generator post-hook, version rollback,
  variant promotion, manual PATCH, pipeline run, backfill command).
- Run inside the Celery loop-bound caches reset wrapper
  (``_run_async_safe``) so it shares the model_router/sqlalchemy hardening
  established by v1.13.
- Never block user-facing chapter save: failures are logged + retried
  asynchronously, never propagated.

Idempotency: the ExtractionMarker is keyed on (project_id, chapter_idx)
with a unique constraint. A second call sees status='completed' and
becomes a no-op. The Marker pattern lives in Neo4j (not Postgres) so we
avoid an alembic migration for v1.5.0; the marker auto-vanishes when the
project is deleted via the existing wipe path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.tasks import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _claim_marker(driver, project_id: str, chapter_idx: int) -> str:
    """Atomically claim the (project, chapter) extraction slot.

    Returns the marker status seen at claim time:
    - 'new'       -> just created, proceed with extraction
    - 'pending'   -> previously created but never completed (retry)
    - 'failed'    -> previous attempt failed, retry
    - 'completed' -> already done, skip
    """
    now = datetime.now(timezone.utc).isoformat()
    async with driver.session() as session:
        result = await session.run(
            "MERGE (m:ExtractionMarker {project_id: $pid, chapter_idx: $idx}) "
            "ON CREATE SET m.status = 'new', m.first_seen = $now, m.attempts = 1 "
            "ON MATCH SET m.attempts = coalesce(m.attempts, 0) + 1, "
            "             m.last_seen = $now "
            "RETURN m.status AS status",
            pid=project_id, idx=int(chapter_idx), now=now,
        )
        record = await result.single()
        return record["status"] if record else "new"


async def _mark_completed(driver, project_id: str, chapter_idx: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with driver.session() as session:
        await session.run(
            "MATCH (m:ExtractionMarker {project_id: $pid, chapter_idx: $idx}) "
            "SET m.status = 'completed', m.completed_at = $now",
            pid=project_id, idx=int(chapter_idx), now=now,
        )


async def _mark_failed(driver, project_id: str, chapter_idx: int, err: str) -> None:
    async with driver.session() as session:
        await session.run(
            "MATCH (m:ExtractionMarker {project_id: $pid, chapter_idx: $idx}) "
            "SET m.status = 'failed', m.last_error = $err",
            pid=project_id, idx=int(chapter_idx), err=err[:500],
        )


async def _ensure_marker_constraint(driver) -> None:
    """Create the ExtractionMarker uniqueness constraint (idempotent)."""
    try:
        async with driver.session() as session:
            await session.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (m:ExtractionMarker) "
                "REQUIRE (m.project_id, m.chapter_idx) IS UNIQUE"
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to ensure ExtractionMarker constraint: %s", e)


async def _load_chapter_text(project_id: str, chapter_idx: int) -> str | None:
    """Load chapter.content_text by (project_id, chapter_idx) via volume join."""
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import Chapter, Volume

    async with async_session_factory() as db:
        # Chapter belongs to a Volume which belongs to a Project.
        stmt = (
            select(Chapter.content_text)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(
                Volume.project_id == str(project_id),
                Chapter.chapter_idx == int(chapter_idx),
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.first()
        return row[0] if row and row[0] else None


async def _extract_chapter_async(
    project_id: str,
    chapter_idx: int,
    caller: str,
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """Core async work: marker -> initialize_graph -> extract_and_update."""
    from app.db.neo4j import init_neo4j, _driver as _existing_driver
    from app.services.entity_timeline import EntityTimelineService

    # Ensure driver is initialised in this celery loop. ``init_neo4j``
    # is idempotent at the per-loop level because the celery wrapper
    # creates a fresh loop per task and ``_driver`` is module-global.
    # We always re-init so we get a driver bound to THIS loop (the
    # previous task's driver belongs to a closed loop).
    await init_neo4j()
    from app.db import neo4j as _neo4j_mod
    driver = _neo4j_mod._driver
    if driver is None:
        return {
            "status": "skipped",
            "reason": "neo4j_unavailable",
            "project_id": project_id,
            "chapter_idx": chapter_idx,
        }

    await _ensure_marker_constraint(driver)
    claim_status = await _claim_marker(driver, project_id, chapter_idx)
    if claim_status == "completed":
        logger.info(
            "entity_extraction skip: already completed (project=%s ch=%d caller=%s)",
            project_id, chapter_idx, caller,
        )
        return {
            "status": "skipped",
            "reason": "already_completed",
            "project_id": project_id,
            "chapter_idx": chapter_idx,
        }

    # Wrap the entire post-claim path so ANY failure (DB load, neo4j init,
    # LLM extract, write) flips the marker to 'failed' before celery retries.
    # Without this, the marker stayed at 'new' on early failures (e.g., the
    # session_factory bug), making it impossible to distinguish 'never ran'
    # from 'ran and failed' when triaging from cypher-shell.
    try:
        chapter_text = await _load_chapter_text(project_id, chapter_idx)
        if not chapter_text:
            await _mark_failed(
                driver, project_id, chapter_idx, "empty_or_missing_content"
            )
            return {
                "status": "skipped",
                "reason": "empty_chapter",
                "project_id": project_id,
                "chapter_idx": chapter_idx,
            }

        service = EntityTimelineService(driver)
        # initialize_graph is idempotent (CREATE CONSTRAINT IF NOT EXISTS)
        # and is the source-of-truth that registers Character /
        # CharacterState / property metadata in Neo4j -- which is precisely
        # what the 49 GqlStatus warnings complained about not existing.
        await service.initialize_graph(project_id)
        await service.extract_and_update(project_id, chapter_idx, chapter_text)
    except Exception as e:
        await _mark_failed(driver, project_id, chapter_idx, repr(e)[:500])
        raise

    await _mark_completed(driver, project_id, chapter_idx)
    return {
        "status": "ok",
        "project_id": project_id,
        "chapter_idx": chapter_idx,
        "caller": caller,
        "prior_marker_status": claim_status,
    }


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    name="entities.extract_chapter",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def extract_chapter_entities(
    self,
    project_id: str,
    chapter_idx: int,
    caller: str = "unknown",
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """Run LLM-driven entity extraction for one chapter and write to Neo4j.

    See module docstring for the full contract. This is the only place that
    actually mutates the graph for chapter-derived character state -- all
    other call sites must use ``dispatch_entity_extraction`` to enqueue
    this task instead of writing directly.
    """
    from app.tasks import _run_async_safe

    return _run_async_safe(
        _extract_chapter_async(
            project_id=str(project_id),
            chapter_idx=int(chapter_idx),
            caller=str(caller),
            chapter_id=str(chapter_id) if chapter_id else None,
        )
    )
