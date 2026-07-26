"""Entity extraction celery task (B2', v1.5.0).

Owns the Neo4j Character/CharacterState/RELATES_TO/AT_LOCATION write path.
For each (project_id, chapter_idx) it:

1. Resolves the target chapter (preferring ``chapter_id``; the dispatched
   ``chapter_idx`` is volume-LOCAL and ambiguous across volumes) and its
   BOOK-GLOBAL index (``Chapter.global_idx``). All downstream keys/values
   (marker, extraction chapter_start, materialized PG projections) live on
   the global axis so volumes never collide.
2. Marks the chapter via an ``ExtractionMarker`` node in Neo4j (atomic
   MERGE with status), keyed on the global idx. Skips work if
   status='completed'.
3. Lazily ensures Neo4j constraints/indexes via
   ``EntityTimelineService.initialize_graph`` (idempotent, no-op once
   constraints exist). This guarantees the read-side query (the source of
   the 49 GqlStatus warnings) sees label/property metadata.
4. Runs ``EntityTimelineService.extract_and_update`` on the chapter's
   ``content_text`` (tier-aware LLM extraction + multi-statement Neo4j
   writes), passing the global idx so timeline edges are book-global.
5. On success flips the marker to ``completed``; on failure flips to
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

Idempotency: the ExtractionMarker is keyed on (project_id, BOOK-GLOBAL
chapter idx) with a unique constraint. A second call sees
status='completed' and becomes a no-op. The Neo4j property is still named
``chapter_idx`` on purpose: volume-1 markers written before the global-axis
fix carried the local idx, which equals the global idx for volume 1, so
they stay valid; volume-2+ chapters get fresh non-colliding keys (their
extraction never actually ran, so re-running heals them). The Marker
pattern lives in Neo4j (not Postgres) so we avoid an alembic migration
for v1.5.0; the marker auto-vanishes when the project is deleted via the
existing wipe path.
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any

from app.tasks import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entity-graph hygiene helpers (pure, deterministic — no LLM)
# ---------------------------------------------------------------------------


def _alias_to_canonical(existing_chars: dict[str, Any]) -> dict[str, str]:
    """Map alias -> canonical character name from profile_json.aliases.

    ``existing_chars`` maps character name -> row-like object with a
    ``profile_json`` attribute. Exact-string aliases only (no fuzzy
    matching). Conservative rules: an alias that equals an existing
    character's own name is never remapped (ambiguous), and an alias
    claimed by two characters keeps the first claimant in sorted-name
    order (deterministic).
    """
    out: dict[str, str] = {}
    for name in sorted(existing_chars):
        profile = getattr(existing_chars[name], "profile_json", None)
        if not isinstance(profile, dict):
            continue
        aliases = profile.get("aliases")
        if not isinstance(aliases, list):
            continue
        for a in aliases:
            if not isinstance(a, str):
                continue
            a = a.strip()
            if not a or a == name or a in existing_chars or a in out:
                continue
            out[a] = name
    return out


def _fold_aliases(
    existing: dict[str, Any],
    char_names: list[str],
    char_profiles: dict[str, dict[str, Any]],
    rels: list[tuple[str, str, str]],
    memberships: list[tuple[str, str, int, int | None]],
    at_locs: list[tuple[str, str, int, int | None]],
    cstates: list[tuple[str, int, int | None, str]],
):
    """Resolve incoming entity names to canonical characters via aliases.

    An incoming name that exactly matches an existing character's
    profile_json.aliases entry is folded into that character everywhere
    (char list, relationship endpoints, memberships, locations, states),
    so no duplicate row is created downstream. A name that already has
    its own row is never remapped.
    """
    alias_of = _alias_to_canonical(existing)
    if not alias_of:
        return char_names, char_profiles, rels, memberships, at_locs, cstates

    def _canon(n: str) -> str:
        return n if n in existing else alias_of.get(n, n)

    def _ce_key(t):
        return (t[0], t[1], t[2], t[3] if t[3] is not None else -1)

    char_names = sorted({_canon(n) for n in char_names})
    # Never overwrite a canonical profile with an alias node's.
    char_profiles = {n: p for n, p in char_profiles.items() if _canon(n) == n}
    rels = sorted({(_canon(s), _canon(t), rt) for (s, t, rt) in rels})
    memberships = sorted(
        {(_canon(c), o, cs, ce) for (c, o, cs, ce) in memberships}, key=_ce_key
    )
    at_locs = sorted(
        {(_canon(c), l, cs, ce) for (c, l, cs, ce) in at_locs}, key=_ce_key
    )
    cstates = sorted(
        {(_canon(c), cs, ce, st) for (c, cs, ce, st) in cstates},
        key=lambda t: (t[0], t[1], t[2] if t[2] is not None else -1, t[3]),
    )
    return char_names, char_profiles, rels, memberships, at_locs, cstates


def _char_bigrams(text: str) -> set[str]:
    s = "".join((text or "").split())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _rule_text_similar(a: str, b: str) -> bool:
    """Deterministic near-duplicate check for world-rule wording.

    True when one text contains the other (only if the shorter side has
    >= 8 non-space chars, so short generic rules never swallow longer
    ones) or when char-bigram Jaccard similarity exceeds 0.8.
    """
    na = "".join((a or "").split())
    nb = "".join((b or "").split())
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 8 and (na in nb or nb in na):
        return True
    ba = _char_bigrams(na)
    bb = _char_bigrams(nb)
    union = ba | bb
    if not union:
        return False
    return len(ba & bb) / len(union) > 0.8


def _plan_world_rule_writes(
    existing: list[tuple[str, str]],
    incoming: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Decide insert-vs-update for incoming (category, rule_text) pairs.

    Returns ``(inserts, updates)`` where inserts are ``(category, text)``
    and updates are ``(category, old_text, new_text)`` against existing
    rows. A rule with the same category AND high textual overlap
    (see ``_rule_text_similar``) as an existing rule is treated as a
    re-worded version of it -> update. Exact duplicates are dropped.
    Different-category or low-similarity rules always insert.
    """
    keys = set(existing)
    by_cat: dict[str, list[str]] = {}
    for cat, txt in existing:
        by_cat.setdefault(cat, []).append(txt)

    inserts: list[tuple[str, str]] = []
    updates: list[tuple[str, str, str]] = []
    for cat, txt in incoming:
        if (cat, txt) in keys:
            continue
        target = next(
            (t for t in by_cat.get(cat, []) if _rule_text_similar(t, txt)),
            None,
        )
        if target is not None:
            keys.discard((cat, target))
            keys.add((cat, txt))
            by_cat[cat] = [txt if t == target else t for t in by_cat[cat]]
            for i, (icat, itxt) in enumerate(inserts):
                if icat == cat and itxt == target:
                    # target was planned this batch: rewrite the insert.
                    inserts[i] = (cat, txt)
                    break
            else:
                updates.append((cat, target, txt))
            continue
        inserts.append((cat, txt))
        keys.add((cat, txt))
        by_cat.setdefault(cat, []).append(txt)
    return inserts, updates


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

        created_chars = 0
        created_rels = 0
        created_rules = 0
        updated_rules = 0
        created_locs = 0
        created_orgs = 0
        created_memberships = 0
        upserted_foreshadows = 0
        created_atlocs = 0
        created_cstates = 0
        skipped_cstates_missing_character = 0
        skipped_cstates_unchanged = 0

        async with async_session_factory() as db:
            # Alias folding: an incoming name that exactly matches an
            # existing character's profile_json.aliases entry is the SAME
            # character (e.g. 「炎帝」 is 「萧炎」). Resolve every incoming
            # name to its canonical form BEFORE any insert so no duplicate
            # row is created and edges/states attach to the canonical row.
            existing_rows = await db.execute(
                select(Character).where(Character.project_id == project_id)
            )
            existing = {c.name: c for c in existing_rows.scalars().all()}
            (
                char_names,
                char_profiles,
                rels,
                memberships,
                at_locs,
                cstates,
            ) = _fold_aliases(
                existing, char_names, char_profiles, rels,
                memberships, at_locs, cstates,
            )

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

            # World rules: exact duplicates skip; a rule with the same
            # category AND high textual overlap with an existing row is a
            # re-worded version -> UPDATE that row's text instead of piling
            # up a contradictory near-duplicate in 【世界规则(不可违反)】.
            # Different-category or low-similarity rules insert as before.
            if world_rules:
                rules_q = await db.execute(
                    select(WorldRule).where(WorldRule.project_id == project_id)
                )
                existing_rule_objs = list(rules_q.scalars().all())
                rule_inserts, rule_updates = _plan_world_rule_writes(
                    [(r.category, r.rule_text) for r in existing_rule_objs],
                    list(world_rules),
                )
                rule_by_key = {
                    (r.category, r.rule_text): r for r in existing_rule_objs
                }
                for cat, old_txt, new_txt in rule_updates:
                    obj = rule_by_key.pop((cat, old_txt), None)
                    if obj is None:
                        continue
                    obj.rule_text = new_txt
                    rule_by_key[(cat, new_txt)] = obj
                    updated_rules += 1
                for cat, txt in rule_inserts:
                    db.add(
                        WorldRule(
                            project_id=project_id, category=cat, rule_text=txt
                        )
                    )
                created_rules += len(rule_inserts)

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
            # source='neo4j' marks rows as Neo4j-materialized so the deletion
            # sync below can tell them apart from PG-only rows (e.g. the
            # foreshadow_lifecycle outline pipeline, which never writes Neo4j).
            if foreshadows:
                fs_rows = []
                for f in foreshadows:
                    fs_rows.append({"project_id": project_id, "source": "neo4j", **f})
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
                        "source": stmt.excluded.source,
                    },
                )
                result = await db.execute(stmt)
                upserted_foreshadows += int(getattr(result, "rowcount", 0) or 0)

            # Foreshadows: deletion sync.
            # Materialize reconciles only rows that originated from Neo4j
            # (source='neo4j', stamped by the upsert above). PG-only rows
            # (foreshadow_lifecycle inserts, legacy rows with source NULL)
            # were never in Neo4j, so their absence there is not deletion.
            neo4j_fs_ids = {f.get("id") for f in foreshadows if f.get("id")}
            pg_fs_rows = await db.execute(
                select(Foreshadow.id).where(
                    Foreshadow.project_id == project_id,
                    Foreshadow.source == "neo4j",
                )
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

            try:
                await db.commit()
            except Exception:
                await db.rollback()

        ENTITY_PG_MATERIALIZE_TOTAL.labels("success", "ok").inc()
        logger.info(
            "entity_pg_materialize ok (project=%s ch=%d caller=%s chars=%d/%d rels=%d/%d rules=%d+%du/%d locs=%d/%d orgs=%d/%d member_of=%d/%d foreshadows=%d/%d atlocs=%d/%d cstates=%d/%d)",
            project_id,
            chapter_idx,
            caller,
            created_chars,
            len(char_names),
            created_rels,
            len(rels),
            created_rules,
            updated_rules,
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
        )
        return {
            "chars_created": created_chars,
            "chars_seen": len(char_names),
            "rels_created": created_rels,
            "rels_seen": len(rels),
            "rules_created": created_rules,
            "rules_updated": updated_rules,
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
            "rules_updated": 0,
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
        }


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _claim_marker(driver, project_id: str, global_idx: int) -> str:
    """Atomically claim the (project, book-global chapter) extraction slot.

    The Neo4j property is still named ``chapter_idx`` for backward
    compatibility with pre-existing volume-1 markers (local == global
    there); the value passed here MUST be the book-global index.

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
            pid=project_id, idx=int(global_idx), now=now,
        )
        record = await result.single()
        return record["status"] if record else "new"


async def _mark_completed(driver, project_id: str, global_idx: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with driver.session() as session:
        await session.run(
            "MATCH (m:ExtractionMarker {project_id: $pid, chapter_idx: $idx}) "
            "SET m.status = 'completed', m.completed_at = $now",
            pid=project_id, idx=int(global_idx), now=now,
        )


async def _mark_failed(driver, project_id: str, global_idx: int, err: str) -> None:
    async with driver.session() as session:
        await session.run(
            "MATCH (m:ExtractionMarker {project_id: $pid, chapter_idx: $idx}) "
            "SET m.status = 'failed', m.last_error = $err",
            pid=project_id, idx=int(global_idx), err=err[:500],
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


async def _resolve_chapter(
    project_id: str,
    chapter_idx: int,
    chapter_id: str | None,
) -> tuple[int, str | None] | None:
    """Resolve the target chapter -> (book-global idx, content_text).

    ``chapter_idx`` is volume-LOCAL (1-based per volume), so on its own it
    is ambiguous across volumes. Resolution therefore prefers
    ``chapter_id`` (passed by every ``dispatch_for_chapter`` persistence
    site and by HookManager). Without it we fall back to the match in the
    HIGHEST volume: id-less dispatches come from the volume currently
    being written, and for single-volume projects the fallback is exact.

    The returned index is ``Chapter.global_idx``; when that column is NULL
    (row predates migration a1001915 in an un-backfilled environment) it
    is computed via ``foreshadow_lifecycle.chapter_global_idx``.

    Returns None when no matching chapter exists.
    """
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import Chapter, Volume

    async with async_session_factory() as db:
        stmt = (
            select(
                Chapter.global_idx,
                Chapter.chapter_idx,
                Chapter.content_text,
                Volume.volume_idx,
            )
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == str(project_id))
        )
        if chapter_id:
            stmt = stmt.where(Chapter.id == str(chapter_id))
        else:
            logger.warning(
                "entity_extraction resolving without chapter_id "
                "(project=%s local_idx=%s): falling back to highest volume",
                project_id, chapter_idx,
            )
            stmt = stmt.where(
                Chapter.chapter_idx == int(chapter_idx)
            ).order_by(Volume.volume_idx.desc())
        row = (await db.execute(stmt.limit(1))).first()
        if row is None:
            return None
        global_idx, local_idx, content, vol_idx = row[0], row[1], row[2], row[3]
        if global_idx is None:
            from app.services.foreshadow_lifecycle import chapter_global_idx

            global_idx = await chapter_global_idx(
                db, str(project_id), int(vol_idx), int(local_idx)
            )
        return int(global_idx), content


async def _extract_chapter_async(
    project_id: str,
    chapter_idx: int,
    caller: str,
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """Core async work: marker -> initialize_graph -> extract_and_update."""
    from app.db.neo4j import init_neo4j
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

    # Resolve the chapter FIRST: the dispatched chapter_idx is volume-local
    # and ambiguous across volumes; the marker and every downstream write
    # are keyed on the book-global index instead.
    resolved = await _resolve_chapter(project_id, chapter_idx, chapter_id)
    if resolved is None:
        logger.warning(
            "entity_extraction skip: chapter not found "
            "(project=%s ch=%s chapter_id=%s caller=%s)",
            project_id, chapter_idx, chapter_id, caller,
        )
        return {
            "status": "skipped",
            "reason": "chapter_not_found",
            "project_id": project_id,
            "chapter_idx": chapter_idx,
        }
    global_idx, chapter_text = resolved

    claim_status = await _claim_marker(driver, project_id, global_idx)
    if claim_status == "completed":
        logger.info(
            "entity_extraction skip: already completed "
            "(project=%s ch=%d global=%d caller=%s)",
            project_id, chapter_idx, global_idx, caller,
        )

        # v1.9: even when extraction is already completed, we still want the
        # Postgres read models to converge. This makes materialization safe to
        # backfill by re-dispatching the extraction task.
        await _materialize_entities_to_postgres(
            project_id=project_id,
            chapter_idx=global_idx,
            caller=caller,
        )
        return {
            "status": "skipped",
            "reason": "already_completed",
            "project_id": project_id,
            "chapter_idx": chapter_idx,
            "global_idx": global_idx,
        }

    # Wrap the entire post-claim path so ANY failure (DB load, neo4j init,
    # LLM extract, write) flips the marker to 'failed' before celery retries.
    # Without this, the marker stayed at 'new' on early failures (e.g., the
    # session_factory bug), making it impossible to distinguish 'never ran'
    # from 'ran and failed' when triaging from cypher-shell.
    try:
        if not chapter_text:
            await _mark_failed(
                driver, project_id, global_idx, "empty_or_missing_content"
            )
            return {
                "status": "skipped",
                "reason": "empty_chapter",
                "project_id": project_id,
                "chapter_idx": chapter_idx,
                "global_idx": global_idx,
            }

        service = EntityTimelineService(driver)
        # initialize_graph is idempotent (CREATE CONSTRAINT IF NOT EXISTS)
        # and is the source-of-truth that registers Character /
        # CharacterState / property metadata in Neo4j -- which is precisely
        # what the 49 GqlStatus warnings complained about not existing.
        await service.initialize_graph(project_id)
        # Book-global idx: Neo4j chapter_start/chapter_end edges (and the PG
        # character_states / character_locations projections materialized
        # from them) all live on the single monotonic global axis.
        await service.extract_and_update(project_id, global_idx, chapter_text)

        # v1.9: materialize Neo4j entity snapshot into Postgres read models.
        await _materialize_entities_to_postgres(
            project_id=project_id,
            chapter_idx=global_idx,
            caller=caller,
        )
    except Exception as e:
        await _mark_failed(driver, project_id, global_idx, repr(e)[:500])
        raise

    await _mark_completed(driver, project_id, global_idx)
    return {
        "status": "ok",
        "project_id": project_id,
        "chapter_idx": chapter_idx,
        "global_idx": global_idx,
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
