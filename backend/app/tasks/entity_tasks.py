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
    from sqlalchemy import delete, select
    from sqlalchemy.dialects.postgresql import insert

    from app.db.neo4j import init_neo4j
    from app.db.session import async_session_factory
    from app.models.project import (
        Character,
        CharacterLocation,
        CharacterOrganization,
        CharacterState,
        Foreshadow,
        Item,            # PR-NEO1 (v2.0)
        ItemEvent,       # PR-NEO1 (v2.0)
        Location,
        Organization,
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

            # Organizations: materialize all Organization node names.
            org_result = await session.run(
                "MATCH (o:Organization {project_id: $pid}) RETURN DISTINCT o.name AS name",
                pid=project_id,
            )
            org_names: list[str] = []
            async for rec in org_result:
                n = rec.get("name") if rec else None
                if isinstance(n, str) and n.strip():
                    org_names.append(n.strip())
            organizations = sorted(set(org_names))

            # MEMBER_OF: materialize all Character-MEMBER_OF->Organization edges.
            member_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[r:MEMBER_OF]->(o:Organization {project_id: $pid}) "
                "RETURN c.name AS cname, o.name AS oname, r.chapter_start AS cs, r.chapter_end AS ce",
                pid=project_id,
            )
            memberships: list[tuple[str, str, int, int | None]] = []
            async for rec in member_result:
                cname = rec.get("cname") if rec else None
                oname = rec.get("oname") if rec else None
                cs = rec.get("cs") if rec else None
                ce = rec.get("ce") if rec else None
                if not isinstance(cname, str) or not cname.strip():
                    continue
                if not isinstance(oname, str) or not oname.strip():
                    continue
                if not isinstance(cs, int):
                    continue
                memberships.append(
                    (
                        cname.strip(),
                        oname.strip(),
                        int(cs),
                        int(ce) if isinstance(ce, int) else None,
                    )
                )
            memberships = sorted(set(memberships))

            # Foreshadows: materialize all Foreshadow nodes.
            fs_result = await session.run(
                "MATCH (f:Foreshadow {project_id: $pid}) "
                "RETURN f.id AS id, f.type AS type, f.description AS description, "
                "       f.planted_chapter AS planted, f.resolve_conditions_json AS conds, "
                "       f.resolution_blueprint_json AS blueprint, f.narrative_proximity AS prox, "
                "       f.status AS status, f.resolved_chapter AS resolved",
                pid=project_id,
            )
            foreshadows: list[dict[str, object]] = []
            async for rec in fs_result:
                fid = rec.get("id") if rec else None
                ftype = rec.get("type") if rec else None
                desc = rec.get("description") if rec else None
                planted = rec.get("planted") if rec else None
                conds = rec.get("conds") if rec else None
                blueprint = rec.get("blueprint") if rec else None
                prox = rec.get("prox") if rec else None
                status = rec.get("status") if rec else None
                resolved = rec.get("resolved") if rec else None

                if not isinstance(fid, str) or not fid.strip():
                    continue
                # PG foreshadows.id is UUID. Skip legacy/non-UUID ids.
                try:
                    import uuid as _uuid

                    fid = str(_uuid.UUID(fid.strip()))
                except Exception:
                    continue
                if not isinstance(ftype, str) or not ftype.strip():
                    continue
                if not isinstance(desc, str) or not desc.strip():
                    continue
                if not isinstance(planted, int):
                    continue

                # Stored as JSON strings in Neo4j; be defensive.
                try:
                    conds_json = json.loads(conds) if isinstance(conds, str) and conds.strip() else []
                except Exception:
                    conds_json = []
                try:
                    blueprint_json = (
                        json.loads(blueprint)
                        if isinstance(blueprint, str) and blueprint.strip()
                        else {}
                    )
                except Exception:
                    blueprint_json = {}

                foreshadows.append(
                    {
                        "id": fid.strip(),
                        "type": ftype.strip(),
                        "description": desc.strip(),
                        "planted_chapter": int(planted),
                        "resolve_conditions_json": conds_json,
                        "resolution_blueprint_json": blueprint_json,
                        "narrative_proximity": float(prox) if isinstance(prox, (int, float)) else 0.0,
                        "status": str(status).strip() if isinstance(status, str) and status.strip() else "planted",
                        "resolved_chapter": int(resolved) if isinstance(resolved, int) else None,
                    }
                )

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
            cstates = sorted(set(cstates), key=lambda t: (t[0], t[1], t[2] if t[2] is not None else -1, t[3]))

            # PR-NEO1 (v2.0): items + item events.
            item_result = await session.run(
                "MATCH (i:Item {project_id: $pid}) "
                "RETURN i.name AS name, i.kind AS kind, i.first_owner AS first_owner",
                pid=project_id,
            )
            item_meta: list[tuple[str, str, str]] = []
            async for rec in item_result:
                n = rec.get("name") if rec else None
                k = rec.get("kind") if rec else None
                fo = rec.get("first_owner") if rec else None
                if not isinstance(n, str) or not n.strip():
                    continue
                item_meta.append((n.strip(), (k or "").strip() if isinstance(k, str) else "", (fo or "").strip() if isinstance(fo, str) else ""))
            item_meta = sorted(set(item_meta))

            # HAS_ITEM events (kind='has') and TRANSFER_ITEM events (kind='transfer').
            has_item_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[r:HAS_ITEM]->(i:Item {project_id: $pid}) "
                "RETURN c.name AS cname, i.name AS iname, r.chapter_start AS cs",
                pid=project_id,
            )
            item_events_neo: list[tuple[str, str, str, str, int, str]] = []
            # tuple shape: (kind, item_name, actor_name, target_name, chapter_idx, note)
            async for rec in has_item_result:
                cname = rec.get("cname") if rec else None
                iname = rec.get("iname") if rec else None
                cs = rec.get("cs") if rec else None
                if not isinstance(cname, str) or not isinstance(iname, str):
                    continue
                if not cname.strip() or not iname.strip() or not isinstance(cs, int):
                    continue
                item_events_neo.append(("has", iname.strip(), cname.strip(), "", int(cs), ""))
            transfer_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[r:TRANSFER_ITEM]->(i:Item {project_id: $pid}) "
                "RETURN c.name AS fname, i.name AS iname, r.to_character AS tname, r.chapter AS cidx, r.reason AS reason",
                pid=project_id,
            )
            async for rec in transfer_result:
                fname = rec.get("fname") if rec else None
                iname = rec.get("iname") if rec else None
                tname = rec.get("tname") if rec else None
                cidx = rec.get("cidx") if rec else None
                reason = rec.get("reason") if rec else None
                if not isinstance(iname, str) or not iname.strip():
                    continue
                if not isinstance(cidx, int):
                    continue
                item_events_neo.append(
                    (
                        "transfer",
                        iname.strip(),
                        (fname or "").strip() if isinstance(fname, str) else "",
                        (tname or "").strip() if isinstance(tname, str) else "",
                        int(cidx),
                        (reason or "").strip() if isinstance(reason, str) else "",
                    )
                )
            item_events_neo = sorted(set(item_events_neo))

        created_chars = 0
        created_rels = 0
        created_rules = 0
        created_locs = 0
        created_orgs = 0
        created_memberships = 0
        upserted_foreshadows = 0
        created_atlocs = 0
        created_cstates = 0
        skipped_cstates_missing_character = 0
        skipped_cstates_unchanged = 0
        created_items = 0           # PR-NEO1 (v2.0)
        created_item_events = 0     # PR-NEO1 (v2.0)

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

            # Deletion sync (v1.9+): keep Postgres relationships consistent with
            # Neo4j truth. If a relationship no longer exists in Neo4j, remove it
            # from the PG read model.
            try:
                # Load all existing PG relationship keys.
                pg_rel_rows = await db.execute(
                    select(Relationship.source_id, Relationship.target_id, Relationship.rel_type).where(
                        Relationship.project_id == project_id
                    )
                )
                pg_keys = {
                    (str(sid), str(tid), str(rt))
                    for sid, tid, rt in pg_rel_rows.all()
                    if sid and tid and rt
                }

                # Build Neo4j relationship keys mapped to PG character ids.
                # (Materialize already created/upserted characters earlier.)
                char_map_rows = await db.execute(
                    select(Character.id, Character.name).where(Character.project_id == project_id)
                )
                name_to_id = {
                    (name or "").strip(): str(cid)
                    for cid, name in char_map_rows.all()
                    if cid and isinstance(name, str) and name.strip()
                }
                neo_keys: set[tuple[str, str, str]] = set()
                for src_name, tgt_name, rel_type in rels:
                    sid = name_to_id.get((src_name or "").strip())
                    tid = name_to_id.get((tgt_name or "").strip())
                    if sid and tid and rel_type:
                        neo_keys.add((sid, tid, rel_type))

                stale = sorted(pg_keys - neo_keys)
                if stale:
                    # Delete row-by-row with a batch of OR clauses.
                    # Relationship count per project is typically small.
                    for sid, tid, rt in stale:
                        await db.execute(
                            delete(Relationship).where(
                                Relationship.project_id == project_id,
                                Relationship.source_id == sid,
                                Relationship.target_id == tid,
                                Relationship.rel_type == rt,
                            )
                        )
            except Exception:
                logger.exception("relationships_deletion_sync_failed")

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

            # Organizations: bulk insert w/ ON CONFLICT DO NOTHING.
            org_rows = [
                {"project_id": project_id, "name": name}
                for name in organizations
            ]
            if org_rows:
                stmt = insert(Organization).values(org_rows)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_organizations_project_name")
                result = await db.execute(stmt)
                created_orgs += int(getattr(result, "rowcount", 0) or 0)

            # Foreshadows: upsert by primary key id.
            if foreshadows:
                fs_rows = []
                for f in foreshadows:
                    fs_rows.append({"project_id": project_id, **f})
                stmt = insert(Foreshadow).values(fs_rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Foreshadow.id],
                    set_={
                        "type": stmt.excluded.type,
                        "description": stmt.excluded.description,
                        "planted_chapter": stmt.excluded.planted_chapter,
                        "resolve_conditions_json": stmt.excluded.resolve_conditions_json,
                        "resolution_blueprint_json": stmt.excluded.resolution_blueprint_json,
                        "narrative_proximity": stmt.excluded.narrative_proximity,
                        "status": stmt.excluded.status,
                        "resolved_chapter": stmt.excluded.resolved_chapter,
                        "project_id": stmt.excluded.project_id,
                    },
                )
                result = await db.execute(stmt)
                upserted_foreshadows += int(getattr(result, "rowcount", 0) or 0)

            # Foreshadows: deletion sync.
            # Materialize is the reconciliation step, so Postgres should not keep
            # foreshadows that were deleted from Neo4j.
            neo4j_fs_ids = {f.get("id") for f in foreshadows if f.get("id")}
            pg_fs_rows = await db.execute(
                select(Foreshadow.id).where(Foreshadow.project_id == project_id)
            )
            pg_fs_ids = {str(r[0]) for r in pg_fs_rows.all()}
            stale_ids = sorted(pg_fs_ids - neo4j_fs_ids)
            if stale_ids:
                await db.execute(
                    delete(Foreshadow).where(
                        Foreshadow.project_id == project_id,
                        Foreshadow.id.in_(stale_ids),
                    )
                )

            # MEMBER_OF: bulk insert projection rows.
            if memberships:
                # Refresh lookup maps (characters + organizations).
                mem_char_rows = await db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.name.in_([c for (c, _, _, _) in memberships]),
                    )
                )
                mem_char_by_name = {c.name: c for c in mem_char_rows.scalars().all()}
                mem_org_rows = await db.execute(
                    select(Organization).where(
                        Organization.project_id == project_id,
                        Organization.name.in_([o for (_, o, _, _) in memberships]),
                    )
                )
                mem_org_by_name = {o.name: o for o in mem_org_rows.scalars().all()}

                mem_rows = []
                for (cname, oname, cs, ce) in memberships:
                    c = mem_char_by_name.get(cname)
                    o = mem_org_by_name.get(oname)
                    if not c or not o:
                        continue
                    mem_rows.append(
                        {
                            "project_id": project_id,
                            "character_id": str(c.id),
                            "organization_id": str(o.id),
                            "chapter_start": int(cs),
                            "chapter_end": int(ce) if ce is not None else None,
                        }
                    )
                if mem_rows:
                    from app.models.project import CharacterOrganization

                    stmt = insert(CharacterOrganization).values(mem_rows)
                    stmt = stmt.on_conflict_do_nothing(
                        constraint="uq_character_organizations_key"
                    )
                    result = await db.execute(stmt)
                    created_memberships += int(getattr(result, "rowcount", 0) or 0)

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

                # PR-OL6: pre-fetch each character's most-recent status_json so we can
                # skip writing duplicates (LLM often echoes "承前" state for unchanged chapters).
                latest_status_by_char: dict[str, str] = {}
                if cs_by_name:
                    char_ids = [str(c.id) for c in cs_by_name.values()]
                    if char_ids:
                        from sqlalchemy import text as _sql_text
                        latest_q = await db.execute(
                            _sql_text(
                                "SELECT DISTINCT ON (character_id) character_id, status_json::text "
                                "FROM character_states WHERE character_id = ANY(:cids) "
                                "ORDER BY character_id, chapter_start DESC, created_at DESC"
                            ),
                            {"cids": char_ids},
                        )
                        for row in latest_q.all():
                            latest_status_by_char[str(row[0])] = str(row[1])

                cs_rows = []
                skipped_cstates_unchanged = 0
                for (cname, cs, ce, status_str) in cstates:
                    c = cs_by_name.get(cname)
                    if not c:
                        skipped_cstates_missing_character += 1
                        continue
                    # PR-OL6: skip if new status_json equals latest persisted state.
                    prev = latest_status_by_char.get(str(c.id))
                    if prev is not None and prev == status_str:
                        skipped_cstates_unchanged += 1
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

            # PR-NEO1 (v2.0): items + item_events upsert.
            if item_meta:
                item_rows_payload = [
                    {
                        "project_id": project_id,
                        "name": n,
                        "kind": k,
                        "first_owner": fo,
                    }
                    for (n, k, fo) in item_meta
                ]
                stmt = insert(Item).values(item_rows_payload)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_items_project_name",
                    set_={
                        "kind": stmt.excluded.kind,
                        "first_owner": stmt.excluded.first_owner,
                    },
                )
                result = await db.execute(stmt)
                created_items += int(getattr(result, "rowcount", 0) or 0)

            # Map item names -> ids for item_events FK.
            item_id_by_name: dict[str, str] = {}
            if item_meta:
                names_only = [n for (n, _, _) in item_meta]
                item_db_rows = await db.execute(
                    select(Item).where(
                        Item.project_id == project_id,
                        Item.name.in_(names_only),
                    )
                )
                for it in item_db_rows.scalars().all():
                    item_id_by_name[it.name] = str(it.id)

            if item_events_neo and item_id_by_name:
                ev_rows: list[dict[str, object]] = []
                for (kind, iname, actor, target, cidx, note) in item_events_neo:
                    iid = item_id_by_name.get(iname)
                    if not iid:
                        continue
                    ev_rows.append(
                        {
                            "project_id": project_id,
                            "item_id": iid,
                            "chapter_idx": int(cidx),
                            "kind": kind,
                            "actor_name": actor,
                            "target_name": target,
                            "note": note,
                        }
                    )
                if ev_rows:
                    stmt = insert(ItemEvent).values(ev_rows)
                    stmt = stmt.on_conflict_do_nothing(constraint="uq_item_events_key")
                    result = await db.execute(stmt)
                    created_item_events += int(getattr(result, "rowcount", 0) or 0)

            try:
                await db.commit()
            except Exception:
                await db.rollback()

        ENTITY_PG_MATERIALIZE_TOTAL.labels("success", "ok").inc()
        logger.info(
            "entity_pg_materialize ok (project=%s ch=%d caller=%s chars=%d/%d rels=%d/%d rules=%d/%d locs=%d/%d orgs=%d/%d member_of=%d/%d foreshadows=%d/%d atlocs=%d/%d cstates=%d/%d items=%d/%d item_events=%d/%d)",
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
            created_orgs,
            len(organizations),
            created_memberships,
            len(memberships),
            upserted_foreshadows,
            len(foreshadows),
            created_atlocs,
            len(at_locs),
            created_cstates,
            len(cstates),
            created_items,           # PR-NEO1
            len(item_meta),          # PR-NEO1
            created_item_events,     # PR-NEO1
            len(item_events_neo),    # PR-NEO1
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
            "orgs_created": created_orgs,
            "orgs_seen": len(organizations),
            "member_of_created": created_memberships,
            "member_of_seen": len(memberships),
            "foreshadows_upserted": upserted_foreshadows,
            "foreshadows_seen": len(foreshadows),
            "atlocs_created": created_atlocs,
            "atlocs_seen": len(at_locs),
            "cstates_created": created_cstates,
            "cstates_seen": len(cstates),
            "cstates_skipped_missing_character": skipped_cstates_missing_character,
            "items_created": created_items,                 # PR-NEO1
            "items_seen": len(item_meta),                   # PR-NEO1
            "item_events_created": created_item_events,     # PR-NEO1
            "item_events_seen": len(item_events_neo),       # PR-NEO1
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
            "orgs_created": 0,
            "orgs_seen": 0,
            "member_of_created": 0,
            "member_of_seen": 0,
            "foreshadows_upserted": 0,
            "foreshadows_seen": 0,
            "atlocs_created": 0,
            "atlocs_seen": 0,
            "cstates_created": 0,
            "cstates_seen": 0,
            "cstates_skipped_missing_character": 0,
            "items_created": 0,                # PR-NEO1
            "items_seen": 0,                   # PR-NEO1
            "item_events_created": 0,          # PR-NEO1
            "item_events_seen": 0,             # PR-NEO1
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
