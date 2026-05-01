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
import json
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
    - Insert `world_rules` by (project_id, category, rule_text)
    - Insert `locations` by (project_id, name)
    - Insert `character_locations` by (project_id, character_id, location_id, chapter_start)
    - Insert `character_states` by (project_id, character_id, chapter_start)

    Best-effort: failures must never break the extraction task.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from app.db.neo4j import init_neo4j
    from app.db.session import async_session_factory
    from app.models.project import (
        Character,
        CharacterLocation,
        CharacterState,
        Location,
        Relationship,
        WorldRule,
    )
    from app.observability.metrics import ENTITY_PG_MATERIALIZE_TOTAL
    from app.services.rel_type import canonicalize_rel_type

    await init_neo4j()
    from app.db import neo4j as _neo4j_mod

    driver = _neo4j_mod._driver
    if driver is None:
        return {
            "chars_created": 0,
            "chars_seen": 0,
            "rels_created": 0,
            "rels_seen": 0,
            "rules_created": 0,
            "rules_seen": 0,
            "locs_created": 0,
            "locs_seen": 0,
        }

    try:
        # IMPORTANT: do NOT use EntityTimelineService.get_world_snapshot() here.
        # That method returns characters only when they have an active HAS_STATE
        # at `chapter_idx`. In real data, new characters can exist as (:Character)
        # nodes without any state edges yet, which would make PG materialization
        # a silent no-op.

        async with driver.session() as session:
            # Characters: materialize all Character nodes.
            # We also pull optional profile_json (string) if present.
            result = await session.run(
                "MATCH (c:Character {project_id: $pid}) "
                "RETURN DISTINCT c.name AS name, c.profile_json AS profile_json",
                pid=project_id,
            )
            char_profiles: dict[str, dict[str, Any]] = {}
            names: list[str] = []
            async for rec in result:
                n = rec.get("name") if rec else None
                p = rec.get("profile_json") if rec else None
                if isinstance(n, str) and n.strip():
                    name = n.strip()
                    names.append(name)
                    # profile_json is optional; store {} if missing/unparseable.
                    if isinstance(p, str) and p.strip():
                        try:
                            obj = json.loads(p)
                            if isinstance(obj, dict):
                                char_profiles[name] = obj
                        except Exception:
                            pass
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
                        rel_type = canonicalize_rel_type(rtype)
                        rels.append((src, tgt, rel_type))

            # World rules: materialize all WorldRule nodes.
            # Neo4j schema (EntityTimelineService): (:WorldRule {project_id, category, text})
            rules_result = await session.run(
                "MATCH (w:WorldRule {project_id: $pid}) RETURN w.category AS category, w.text AS text",
                pid=project_id,
            )
            rules: list[tuple[str, str]] = []
            async for rec in rules_result:
                cat = rec.get("category") if rec else None
                txt = rec.get("text") if rec else None
                if isinstance(cat, str) and isinstance(txt, str):
                    cat = cat.strip()
                    txt = txt.strip()
                    if cat and txt:
                        rules.append((cat, txt))
            world_rules = sorted(set(rules))

            # Locations: materialize all Location node names.
            loc_result = await session.run(
                "MATCH (l:Location {project_id: $pid}) RETURN DISTINCT l.name AS name",
                pid=project_id,
            )
            loc_names: list[str] = []
            async for rec in loc_result:
                n = rec.get("name") if rec else None
                if isinstance(n, str) and n.strip():
                    loc_names.append(n.strip())
            locations = sorted(set(loc_names))

            # AT_LOCATION: materialize all Character-AT_LOCATION->Location edges.
            # Neo4j schema (EntityTimelineService):
            #   (c:Character)-[:AT_LOCATION {chapter_start, chapter_end}]->(l:Location)
            atloc_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[r:AT_LOCATION]->(l:Location {project_id: $pid}) "
                "RETURN c.name AS cname, l.name AS lname, r.chapter_start AS cs, r.chapter_end AS ce",
                pid=project_id,
            )
            at_locs: list[tuple[str, str, int, int | None]] = []
            async for rec in atloc_result:
                cname = rec.get("cname") if rec else None
                lname = rec.get("lname") if rec else None
                cs = rec.get("cs") if rec else None
                ce = rec.get("ce") if rec else None
                if not isinstance(cname, str) or not cname.strip():
                    continue
                if not isinstance(lname, str) or not lname.strip():
                    continue
                if not isinstance(cs, int):
                    continue
                at_locs.append((cname.strip(), lname.strip(), int(cs), int(ce) if isinstance(ce, int) else None))

            # Deduplicate by key.
            at_locs = sorted(set(at_locs))

            # HAS_STATE: materialize all Character-HAS_STATE->CharacterState nodes.
            # Neo4j schema (EntityTimelineService):
            #   (c:Character)-[:HAS_STATE]->(s:CharacterState {chapter_start, chapter_end, status_json})
            cs_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[:HAS_STATE]->(s:CharacterState) "
                "RETURN c.name AS cname, s.chapter_start AS cs, s.chapter_end AS ce, s.status_json AS status "
                "ORDER BY c.name, s.chapter_start",
                pid=project_id,
            )
            cstates: list[tuple[str, int, int | None, str]] = []
            async for rec in cs_result:
                cname = rec.get("cname") if rec else None
                cs = rec.get("cs") if rec else None
                ce = rec.get("ce") if rec else None
                status = rec.get("status") if rec else None
                if not isinstance(cname, str) or not cname.strip():
                    continue
                if not isinstance(cs, int):
                    continue
                status_str = status if isinstance(status, str) else ("{}" if status is None else str(status))
                cstates.append(
                    (
                        cname.strip(),
                        int(cs),
                        int(ce) if isinstance(ce, int) else None,
                        status_str,
                    )
                )
            cstates = sorted(set(cstates))

        created_chars = 0
        created_rels = 0
        created_rules = 0
        created_locs = 0
        created_atlocs = 0
        created_cstates = 0
        skipped_cstates_missing_character = 0

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
                        profile_json=char_profiles.get(name, {}),
                    )
                )
                created_chars += 1

            await db.flush()

            # Update existing character profile_json from Neo4j when available.
            # This keeps PG read models in sync even when characters were
            # originally created by legacy PG-only paths.
            if char_names and char_profiles:
                all_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_(char_names),
                    )
                )
                for c in all_rows.scalars().all():
                    new_profile = char_profiles.get(c.name)
                    if (
                        isinstance(new_profile, dict)
                        and new_profile
                        and c.profile_json != new_profile
                    ):
                        c.profile_json = new_profile
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

            # Relationships: bulk insert w/ ON CONFLICT DO NOTHING.
            rel_rows = []
            for (src_name, tgt_name, rel_type) in rels:
                src = by_name.get(src_name)
                tgt = by_name.get(tgt_name)
                if not src or not tgt:
                    continue
                rel_rows.append(
                    {
                        "project_id": project_id,
                        "source_id": str(src.id),
                        "target_id": str(tgt.id),
                        "rel_type": rel_type,
                    }
                )
            if rel_rows:
                stmt = insert(Relationship).values(rel_rows)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_relationships_rel_key")
                result = await db.execute(stmt)
                created_rels += int(getattr(result, "rowcount", 0) or 0)

            # World rules: bulk insert w/ ON CONFLICT DO NOTHING.
            rule_rows = [
                {"project_id": project_id, "category": cat, "rule_text": txt}
                for (cat, txt) in world_rules
            ]
            if rule_rows:
                stmt = insert(WorldRule).values(rule_rows)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_world_rules_key")
                result = await db.execute(stmt)
                created_rules += int(getattr(result, "rowcount", 0) or 0)

            # Locations: bulk insert w/ ON CONFLICT DO NOTHING.
            loc_rows = [
                {"project_id": project_id, "name": name}
                for name in locations
            ]
            if loc_rows:
                stmt = insert(Location).values(loc_rows)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_locations_project_name")
                result = await db.execute(stmt)
                created_locs += int(getattr(result, "rowcount", 0) or 0)

            # AT_LOCATION: bulk insert projection rows.
            if at_locs:
                # Refresh lookup maps (characters + locations).
                char_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_([c for (c, _, _, _) in at_locs]),
                    )
                )
                char_by_name = {c.name: c for c in char_rows.scalars().all()}
                loc_rows_db = await db.execute(
                    select(Location).where(
                        Location.project_id == project_id,
                        Location.name.in_([l for (_, l, _, _) in at_locs]),
                    )
                )
                loc_by_name = {l.name: l for l in loc_rows_db.scalars().all()}

                atloc_rows = []
                for (cname, lname, cs, ce) in at_locs:
                    c = char_by_name.get(cname)
                    l = loc_by_name.get(lname)
                    if not c or not l:
                        continue
                    atloc_rows.append(
                        {
                            "project_id": project_id,
                            "character_id": str(c.id),
                            "location_id": str(l.id),
                            "chapter_start": int(cs),
                            "chapter_end": int(ce) if ce is not None else None,
                        }
                    )
                if atloc_rows:
                    stmt = insert(CharacterLocation).values(atloc_rows)
                    stmt = stmt.on_conflict_do_nothing(constraint="uq_character_locations_key")
                    result = await db.execute(stmt)
                    created_atlocs += int(getattr(result, "rowcount", 0) or 0)

            # HAS_STATE: bulk insert projection rows.
            if cstates:
                import uuid as _uuid

                cs_char_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_([c for (c, _, _, _) in cstates]),
                    )
                )
                cs_by_name = {c.name: c for c in cs_char_rows.scalars().all()}

                cs_rows = []
                for (cname, cs, ce, status_str) in cstates:
                    c = cs_by_name.get(cname)
                    if not c:
                        skipped_cstates_missing_character += 1
                        continue
                    cs_rows.append(
                        {
                            "id": str(_uuid.uuid4()),
                            "project_id": project_id,
                            "character_id": str(c.id),
                            "chapter_start": int(cs),
                            "chapter_end": int(ce) if ce is not None else None,
                            "status_json": status_str,
                        }
                    )

                if cs_rows:
                    stmt = insert(CharacterState).values(cs_rows)
                    stmt = stmt.on_conflict_do_nothing(constraint="uq_character_states_key")
                    result = await db.execute(stmt)
                    created_cstates += int(getattr(result, "rowcount", 0) or 0)

            try:
                await db.commit()
            except Exception:
                await db.rollback()

        ENTITY_PG_MATERIALIZE_TOTAL.labels("success", "ok").inc()
        logger.info(
            "entity_pg_materialize ok (project=%s ch=%d caller=%s chars=%d/%d rels=%d/%d rules=%d/%d locs=%d/%d atlocs=%d/%d cstates=%d/%d)",
            project_id,
            chapter_idx,
            caller,
            created_chars,
            len(char_names),
            created_rels,
            len(rels),
            created_rules,
            len(world_rules),
            created_locs,
            len(locations),
            created_atlocs,
            len(at_locs),
            created_cstates,
            len(cstates),
        )
        return {
            "chars_created": created_chars,
            "chars_seen": len(char_names),
            "rels_created": created_rels,
            "rels_seen": len(rels),
            "rules_created": created_rules,
            "rules_seen": len(world_rules),
            "locs_created": created_locs,
            "locs_seen": len(locations),
            "atlocs_created": created_atlocs,
            "atlocs_seen": len(at_locs),
            "cstates_created": created_cstates,
            "cstates_seen": len(cstates),
            "cstates_skipped_missing_character": skipped_cstates_missing_character,
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
        return {
            "chars_created": 0,
            "chars_seen": 0,
            "rels_created": 0,
            "rels_seen": 0,
            "rules_created": 0,
            "rules_seen": 0,
            "locs_created": 0,
            "locs_seen": 0,
            "atlocs_created": 0,
            "atlocs_seen": 0,
            "cstates_created": 0,
            "cstates_seen": 0,
            "cstates_skipped_missing_character": 0,
        }


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
