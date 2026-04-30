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


async def _materialize_entities_to_postgres(
    *,
    project_id: str,
    chapter_idx: int,
    caller: str,
) -> dict[str, int]:
    """Materialize Neo4j entity snapshot into Postgres.

    Minimal v1.9 scope:
    - Upsert `characters` by (project_id, name)
    - Insert `relationships` by (project_id, source_id, target_id, rel_type)

    Best-effort: failures must never break the extraction task.
    """
    from sqlalchemy import select

    from app.db.neo4j import init_neo4j
    from app.db.session import async_session_factory
    from app.models.project import Character, Relationship
    from app.observability.metrics import ENTITY_PG_MATERIALIZE_TOTAL

    await init_neo4j()
    from app.db import neo4j as _neo4j_mod

    driver = _neo4j_mod._driver
    if driver is None:
        return {"chars_created": 0, "chars_seen": 0, "rels_created": 0, "rels_seen": 0}

    try:
        # IMPORTANT: do NOT use EntityTimelineService.get_world_snapshot() here.
        # That method returns characters only when they have an active HAS_STATE
        # at `chapter_idx`. In real data, new characters can exist as (:Character)
        # nodes without any state edges yet, which would make PG materialization
        # a silent no-op.

        async with driver.session() as session:
            # Characters: materialize all Character node names.
            result = await session.run(
                "MATCH (c:Character {project_id: $pid}) RETURN DISTINCT c.name AS name",
                pid=project_id,
            )
            names: list[str] = []
            async for rec in result:
                n = rec.get("name") if rec else None
                if isinstance(n, str) and n.strip():
                    names.append(n)
            char_names = sorted(set(names))

            # Relationships: materialize all RELATES_TO edges.
            rel_result = await session.run(
                "MATCH (a:Character {project_id: $pid})-[r:RELATES_TO]->(b:Character {project_id: $pid}) "
                "RETURN a.name AS source, b.name AS target, r.type AS rtype",
                pid=project_id,
            )
            rels: list[tuple[str, str, str]] = []
            async for rec in rel_result:
                src = rec.get("source") if rec else None
                tgt = rec.get("target") if rec else None
                rtype = rec.get("rtype") if rec else None
                if isinstance(src, str) and isinstance(tgt, str) and isinstance(rtype, str):
                    if src.strip() and tgt.strip() and rtype.strip():
                        # Normalize rel_type to keep it short/stable (see spec §8).
                        raw_rel_type = rtype.strip()
                        rel_type = raw_rel_type
                        if "（" in rel_type:
                            rel_type = rel_type.split("（", 1)[0].strip()
                        if "(" in rel_type:
                            rel_type = rel_type.split("(", 1)[0].strip()
                        if "/" in rel_type:
                            rel_type = rel_type.split("/", 1)[0].strip()
                        if any(k in raw_rel_type for k in ["敌对", "仇敌", "死敌"]):
                            rel_type = "敌对"
                        elif any(k in raw_rel_type for k in ["对立", "不信任", "对手"]):
                            rel_type = "对立"
                        # Regulatory / enforcement actions are treated as 监管.
                        elif any(k in raw_rel_type for k in ["监管", "押解", "押送", "看押", "管辖", "盘查", "监控", "审查", "取证", "查档", "查档对照"]):
                            rel_type = "监管"
                        elif any(k in raw_rel_type for k in ["审讯", "逼问"]):
                            rel_type = "审讯"
                        elif any(k in raw_rel_type for k in ["师生", "师徒"]):
                            rel_type = "师生"
                        elif any(k in raw_rel_type for k in ["上下级", "上位", "下属"]):
                            rel_type = "上下级"
                        elif any(k in raw_rel_type for k in ["同舍", "同寝"]):
                            rel_type = "同舍"
                        elif any(k in raw_rel_type for k in ["同伴", "同学", "同行", "协作"]):
                            rel_type = "同伴"
                        elif any(k in raw_rel_type for k in ["失联", "寻找"]):
                            rel_type = "失联"
                        rel_type = (rel_type or "other")[:50]
                        rels.append((src, tgt, rel_type))

        created_chars = 0
        created_rels = 0

        async with async_session_factory() as db:
            if char_names:
                existing_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_(char_names),
                    )
                )
                existing = {c.name: c for c in existing_rows.scalars().all()}
            else:
                existing = {}

            for name in char_names:
                if name in existing:
                    continue
                db.add(
                    Character(
                        project_id=project_id,
                        name=name,
                        profile_json={},
                    )
                )
                created_chars += 1

            await db.flush()

            if char_names:
                all_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_(char_names),
                    )
                )
                by_name = {c.name: c for c in all_rows.scalars().all()}
            else:
                by_name = {}

            for (src_name, tgt_name, rel_type) in rels:
                src = by_name.get(src_name)
                tgt = by_name.get(tgt_name)
                if not src or not tgt:
                    continue

                # Use a SAVEPOINT so unique-constraint conflicts don't abort the whole transaction.
                try:
                    async with db.begin_nested():
                        db.add(
                            Relationship(
                                project_id=project_id,
                                source_id=src.id,
                                target_id=tgt.id,
                                rel_type=rel_type,
                            )
                        )
                        await db.flush()
                        created_rels += 1
                except Exception:
                    # Treat unique constraint conflicts as already-created.
                    continue

            try:
                await db.commit()
            except Exception:
                await db.rollback()

        ENTITY_PG_MATERIALIZE_TOTAL.labels("success", "ok").inc()
        logger.info(
            "entity_pg_materialize ok (project=%s ch=%d caller=%s chars=%d/%d rels=%d/%d)",
            project_id,
            chapter_idx,
            caller,
            created_chars,
            len(char_names),
            created_rels,
            len(rels),
        )
        return {
            "chars_created": created_chars,
            "chars_seen": len(char_names),
            "rels_created": created_rels,
            "rels_seen": len(rels),
        }
    except Exception as e:
        ENTITY_PG_MATERIALIZE_TOTAL.labels("failure", e.__class__.__name__).inc()
        logger.error(
            "entity_pg_materialize failed (project=%s ch=%d caller=%s): %s",
            project_id,
            chapter_idx,
            caller,
            e,
            exc_info=True,
        )
        return {"chars_created": 0, "chars_seen": 0, "rels_created": 0, "rels_seen": 0}


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

        # v1.9: even when extraction is already completed, we still want the
        # Postgres read models to converge. This makes materialization safe to
        # backfill by re-dispatching the extraction task.
        await _materialize_entities_to_postgres(
            project_id=project_id,
            chapter_idx=chapter_idx,
            caller=caller,
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

        # v1.9: materialize Neo4j entity snapshot into Postgres read models.
        await _materialize_entities_to_postgres(
            project_id=project_id,
            chapter_idx=chapter_idx,
            caller=caller,
        )
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
