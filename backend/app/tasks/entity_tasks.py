"""Source-bound chapter entity extraction and durable projection tasks.

Chapter identity and persisted global order are resolved before any graph IO.
Validated payloads live in PostgreSQL; graph replacement is atomic and fenced.
Legacy global-index ExtractionMarker nodes no longer grant permission to skip.
"""

from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

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


def _plan_world_rule_writes(
    existing: list[tuple[str, str]],
    incoming: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Only exact repeated registrations are duplicates.

    Similar wording cannot prove that a restriction, condition or exception
    replaces another rule. Preserve both until an explicit author edit.
    The legacy two-list return shape remains; inferred updates are always empty.
    """
    keys = set(existing)
    inserts: list[tuple[str, str]] = []
    for cat, txt in incoming:
        if (cat, txt) in keys:
            continue
        inserts.append((cat, txt))
        keys.add((cat, txt))
    return inserts, []


# ---------------------------------------------------------------------------
# Tier-2 memory cards (定位层) — deterministic verbatim excerpts, zero LLM
# ---------------------------------------------------------------------------

_MEMORY_CARD_MAX_CHARS = 300
_SENTENCE_ENDERS = "。！？…!?\n"


def _name_occurrences(text: str, name: str) -> list[int]:
    """Non-overlapping start positions of ``name`` in ``text`` (in order)."""
    positions: list[int] = []
    start = 0
    while True:
        pos = text.find(name, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + len(name)
    return positions


def _sentence_bounded_excerpt(
    text: str,
    pos: int,
    token_len: int,
    max_chars: int = _MEMORY_CARD_MAX_CHARS,
) -> str:
    """Sentence-bounded window around ``text[pos:pos+token_len]``.

    Deterministic: expand to the enclosing sentence, then greedily grow by
    whole sentences (forward first, then backward) while the window fits in
    ``max_chars``. A single sentence longer than ``max_chars`` is hard-trimmed
    around the occurrence instead.
    """
    n = len(text)
    start = pos
    while start > 0 and text[start - 1] not in _SENTENCE_ENDERS:
        start -= 1
    end = min(pos + token_len, n)
    while end < n and text[end] not in _SENTENCE_ENDERS:
        end += 1
    if end < n:
        end += 1  # include the sentence terminator

    if end - start > max_chars:
        half = max_chars // 2
        s = max(start, pos - half)
        e = min(end, s + max_chars)
        s = max(start, e - max_chars)
        return text[s:e].strip()

    while True:
        # Forward: one more whole sentence.
        ext_end = end
        while ext_end < n and text[ext_end] not in _SENTENCE_ENDERS:
            ext_end += 1
        if ext_end < n:
            ext_end += 1
        if ext_end > end and (ext_end - start) <= max_chars:
            end = ext_end
            continue
        # Backward: one more whole sentence.
        ext_start = start
        if ext_start > 0:
            ext_start -= 1  # step over the previous terminator
            while ext_start > 0 and text[ext_start - 1] not in _SENTENCE_ENDERS:
                ext_start -= 1
        if ext_start < start and (end - ext_start) <= max_chars:
            start = ext_start
            continue
        break
    return text[start:end].strip()


def plan_memory_cards(
    chapter_text: str,
    global_idx: int,
    char_names: list[str],
    first_seen_by_name: dict[str, int],
) -> list[dict[str, Any]]:
    """Plan the memory-card rows for one chapter. Pure and deterministic.

    Per appearing character (exact canonical-name occurrence, len >= 2):
    - ``first_appearance`` card from the FIRST occurrence's sentence window,
      when no verified earlier mention exists. This legacy storage label
      marks an early excerpt, not proof of a character's first entrance.
    - ``key_moment`` card from the occurrence nearest the chapter midpoint
      (skipped when it would duplicate the first_appearance excerpt).

    Returns ``[{"character_name", "global_idx", "card_type", "excerpt"}]``
    sorted by (character_name, card_type) for stable output.
    """
    if not chapter_text:
        return []
    rows: list[dict[str, Any]] = []
    midpoint = len(chapter_text) // 2
    for name in sorted({n for n in char_names if n and len(n) >= 2}):
        occ = _name_occurrences(chapter_text, name)
        if not occ:
            continue
        first_pos = occ[0]
        mid_pos = min(occ, key=lambda p: (abs(p - midpoint), p))
        first_seen = first_seen_by_name.get(name)
        is_first_chapter = first_seen is None or int(first_seen) >= int(global_idx)

        first_excerpt = _sentence_bounded_excerpt(chapter_text, first_pos, len(name))
        mid_excerpt = _sentence_bounded_excerpt(chapter_text, mid_pos, len(name))
        if is_first_chapter:
            if first_excerpt:
                rows.append(
                    {
                        "character_name": name,
                        "global_idx": int(global_idx),
                        "card_type": "first_appearance",
                        "excerpt": first_excerpt,
                    }
                )
            if mid_excerpt and mid_excerpt != first_excerpt:
                rows.append(
                    {
                        "character_name": name,
                        "global_idx": int(global_idx),
                        "card_type": "key_moment",
                        "excerpt": mid_excerpt,
                    }
                )
        elif mid_excerpt:
            rows.append(
                {
                    "character_name": name,
                    "global_idx": int(global_idx),
                    "card_type": "key_moment",
                    "excerpt": mid_excerpt,
                }
            )
    return rows


def plan_card_eviction(
    cards: list[tuple[Any, str, int]],
    cap: int,
) -> list[Any]:
    """Card ids to delete so one character keeps at most ``cap`` cards.

    ``cards`` is ``[(id, card_type, global_idx), ...]`` for ONE character.
    Keep one earliest anchor, then the most recent other excerpts. Legacy
    first_appearance labels are only a tie-breaker, never an unlimited pin.
    Callers must exclude invalid source rows before applying this policy.
    """
    if cap <= 0 or len(cards) <= cap:
        return []
    ordered = sorted(cards, key=lambda c: (c[2], c[1] != "first_appearance", str(c[0])))
    anchor = ordered[0]
    remaining = sorted(ordered[1:], key=lambda c: (-c[2], c[1] != "first_appearance", str(c[0])))
    keep = {anchor[0], *(c[0] for c in remaining[:cap - 1])}
    return [c[0] for c in ordered if c[0] not in keep]



async def _upsert_memory_cards(
    *,
    project_id: str,
    global_idx: int,
    chapter_text: str,
    source_chapter_id: str | None = None,
    source_hash: str | None = None,
) -> dict[str, int]:
    """Tier-2 write path: deterministic memory-card upsert for one chapter.

    Failures propagate to the versioned extraction worker for durable replay.
    New worker calls bind each excerpt to its chapter identity and source.
    Idempotent: re-extraction overwrites the same (project, name, chapter,
    type) rows and retention converges to the same card set.
    """
    from sqlalchemy import delete, select, or_
    from sqlalchemy.dialects.postgresql import insert

    from app.config import settings
    from app.db.session import async_session_factory
    from app.models.project import (
        Character,
        CharacterMemoryCard,
    )

    cap = int(getattr(settings, "MEMORY_CARDS_PER_CHARACTER", 10))

    async with async_session_factory() as db:
        char_rows = await db.execute(
            select(Character.name).where(Character.project_id == project_id)
        )
        char_names = [n for (n,) in char_rows.all() if isinstance(n, str) and n.strip()]
        from app.services.character_roster import load_historical_roster
        historical = await load_historical_roster(db, project_id, before_global_idx=global_idx)
        first_seen_by_name = {row['character_name']: row['first_seen_chapter'] for row in historical}

        planned = plan_memory_cards(
            chapter_text, int(global_idx), char_names, first_seen_by_name
        )
        await db.execute(delete(CharacterMemoryCard).where(
            CharacterMemoryCard.project_id == project_id,
            or_(CharacterMemoryCard.global_idx == global_idx,
                CharacterMemoryCard.source_chapter_id == source_chapter_id)
            if source_chapter_id else CharacterMemoryCard.global_idx == global_idx,
        ))
        for row in planned:
            stmt = insert(CharacterMemoryCard).values(
                project_id=project_id,
                character_name=row["character_name"],
                global_idx=row["global_idx"],
                card_type=row["card_type"],
                excerpt=row["excerpt"][:_MEMORY_CARD_MAX_CHARS],
                source_chapter_id=source_chapter_id,
                source_hash=source_hash,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_character_memory_cards_key",
                set_={"excerpt": stmt.excluded.excerpt,
                      "source_chapter_id": stmt.excluded.source_chapter_id,
                      "source_hash": stmt.excluded.source_hash},
            )
            await db.execute(stmt)

        # Invalid/unknown sources must not occupy retention slots.
        evicted = 0
        for name in sorted({r["character_name"] for r in planned}):
            card_rows = await db.execute(
                select(
                    CharacterMemoryCard.id,
                    CharacterMemoryCard.card_type,
                    CharacterMemoryCard.global_idx,
                    CharacterMemoryCard.source_chapter_id,
                    CharacterMemoryCard.source_hash,
                    CharacterMemoryCard.excerpt,
                ).where(
                    CharacterMemoryCard.project_id == project_id,
                    CharacterMemoryCard.character_name == name,
                )
            )
            from app.services.entity_sources import filter_current_entity_rows
            candidates = [dict(id=cid, card_type=ct, global_idx=gi,
                               source_chapter_id=sid, source_hash=shash, excerpt=excerpt)
                          for cid, ct, gi, sid, shash, excerpt in card_rows.all()]
            boundary = max((row['global_idx'] for row in candidates), default=0) + 1
            valid = await filter_current_entity_rows(db, project_id, candidates,
                                                     before_global_idx=boundary)
            valid_ids = {row['id'] for row in valid}
            doomed = [row['id'] for row in candidates if row['id'] not in valid_ids]
            doomed += plan_card_eviction(
                [(row['id'], row['card_type'], row['global_idx']) for row in valid], cap)
            if doomed:
                await db.execute(
                    delete(CharacterMemoryCard).where(
                        CharacterMemoryCard.id.in_(doomed)
                    )
                )
                evicted += len(doomed)

        await db.commit()

    logger.info(
        "memory_cards upserted (project=%s global=%d cards=%d evicted=%d)",
        project_id, int(global_idx), len(planned), evicted,
    )
    return {"cards_planned": len(planned), "cards_evicted": evicted}


async def _materialize_entities_to_postgres(
    *,
    project_id: str,
    chapter_idx: int,
    caller: str,
    strict: bool = False,
    graph_driver=None,
    preserve_existing_profiles: bool = False,
) -> dict[str, int]:
    """Serialize cleanup and snapshot writes in one PostgreSQL transaction."""
    from app.db.session import async_session_factory
    from app.db import neo4j
    from app.services.entity_materialization_lock import require_materialization_lock, MaterializationBusy
    from app.services.character_deletion import stage_character_deletions, acknowledge_character_deletions
    try:
        async with async_session_factory() as db:
            await require_materialization_lock(db, project_id)
            if graph_driver is None:
                await neo4j.init_neo4j()
                graph_driver = neo4j._driver
            if graph_driver is None:
                raise RuntimeError('graph_unavailable')
            tokens = await stage_character_deletions(db, graph_driver, project_id)
            result = await _materialize_entity_snapshot(db, project_id=project_id,
                chapter_idx=chapter_idx, caller=caller, strict=True, graph_driver=graph_driver,
                preserve_existing_profiles=preserve_existing_profiles)
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            await acknowledge_character_deletions(graph_driver, project_id, tokens)
            from app.observability.metrics import ENTITY_PG_MATERIALIZE_TOTAL
            ENTITY_PG_MATERIALIZE_TOTAL.labels("success", "ok").inc()
            return result
    except Exception as exc:
        if strict or isinstance(exc, MaterializationBusy):
            raise
        logger.warning('entity materialization incomplete: %s', type(exc).__name__)
        return {key: 0 for key in (
            'chars_created', 'chars_seen', 'rels_created', 'rels_seen', 'rules_created', 'rules_updated', 'rules_seen',
            'locs_created', 'locs_seen', 'orgs_created', 'orgs_seen', 'member_of_created', 'member_of_seen',
            'foreshadows_upserted', 'foreshadows_seen', 'atlocs_created', 'atlocs_seen',
            'cstates_created', 'cstates_seen', 'cstates_skipped_missing_character')}


async def _materialize_entity_snapshot(
    db, *,
    project_id: str,
    chapter_idx: int,
    caller: str,
    strict: bool = False,
    graph_driver=None,
    preserve_existing_profiles: bool = False,
) -> dict[str, int]:
    """Materialize Neo4j entity snapshot into Postgres.

    Minimal v1.9 scope:
    - Upsert `characters` by (project_id, name)
    - Insert `relationships` by (project_id, source_id, target_id, rel_type)
    - Insert `world_rules` by (project_id, category, rule_text)
    - Insert `locations` by (project_id, name)
    - Insert `character_locations` by (project_id, character_id, location_id, chapter_start)
    - Insert `character_states` by (project_id, character_id, chapter_start)

    strict=True propagates failures to the durable worker; standalone legacy
    callers retain best-effort behavior.
    preserve_existing_profiles=True is for outline settings imports: new graph
    characters are projected, while existing author cards remain untouched.
    """
    from app.services.entity_bulk import upsert_projection_rows
    from app.services.character_profile_authority import projected_profile
    from sqlalchemy import delete, select

    from app.db.neo4j import init_neo4j
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

    if graph_driver is None:
        await init_neo4j()
        from app.db import neo4j as _neo4j_mod
        driver = _neo4j_mod._driver
    else:
        driver = graph_driver
    if driver is None:
        if strict:
            raise RuntimeError("Neo4j unavailable during materialization")
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
                "RETURN DISTINCT c.name AS name, c.profile_json AS profile_json, c.profile_origin AS profile_origin",
                pid=project_id,
            )
            char_profiles: dict[str, dict[str, Any]] = {}
            char_profile_origins: dict[str, str | None] = {}
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
                                char_profile_origins[name] = rec.get('profile_origin')
                        except Exception:
                            pass
            char_names = sorted(set(names))

            # Relationships: materialize all RELATES_TO edges.
            rel_result = await session.run(
                "MATCH (a:Character {project_id: $pid})-[r:RELATES_TO]->(b:Character {project_id: $pid}) "
                "RETURN a.name AS source, b.name AS target, r.type AS rtype, "
                "elementId(r) AS edge_id,properties(r) AS properties",
                pid=project_id,
            )
            rels: list[tuple[str, str, str]] = []
            relationship_records = []
            async for rec in rel_result:
                src = rec.get("source") if rec else None
                tgt = rec.get("target") if rec else None
                rtype = rec.get("rtype") if rec else None
                if isinstance(src, str) and isinstance(tgt, str) and isinstance(rtype, str):
                    if src.strip() and tgt.strip() and rtype.strip():
                        rel_type = canonicalize_rel_type(rtype)
                        rels.append((src, tgt, rel_type))
                        relationship_records.append(dict(source=src.strip(), target=tgt.strip(),
                            edge_id=rec.get('edge_id') or '', properties=rec.get('properties') or {'type': rtype}))

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
                "RETURN c.name AS cname, l.name AS lname, r.chapter_start AS cs, r.chapter_end AS ce, "
                "r.source_chapter_id AS source_chapter_id, r.source_hash AS source_hash",
                pid=project_id,
            )
            location_sources = {}
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
                location_sources[(cname.strip(), lname.strip(), int(cs))] = (rec.get("source_chapter_id"), rec.get("source_hash"))
                at_locs.append((cname.strip(), lname.strip(), int(cs), int(ce) if isinstance(ce, int) else None))

            # Deduplicate by key.
            at_locs = sorted(set(at_locs))

            # HAS_STATE: materialize all Character-HAS_STATE->CharacterState nodes.
            # Neo4j schema (EntityTimelineService):
            #   (c:Character)-[:HAS_STATE]->(s:CharacterState {chapter_start, chapter_end, status_json})
            cs_result = await session.run(
                "MATCH (c:Character {project_id: $pid})-[:HAS_STATE]->(s:CharacterState) "
                "WHERE s.chapter_end IS NULL OR s.chapter_end >= s.chapter_start "
                "RETURN c.name AS cname, s.chapter_start AS cs, s.chapter_end AS ce, s.status_json AS status, "
                "s.source_chapter_id AS source_chapter_id, s.source_hash AS source_hash, s.source_mode AS source_mode "
                "ORDER BY c.name, s.chapter_start",
                pid=project_id,
            )
            state_sources = {}
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
                state_sources[(cname.strip(), int(cs))] = (rec.get("source_chapter_id"), rec.get("source_hash"), rec.get("source_mode"))
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

        # Alias folding: an incoming name that exactly matches an
        # existing character's profile_json.aliases entry is the SAME
        # character (e.g. 「炎帝」 is 「萧炎」). Resolve every incoming
        # name to its canonical form BEFORE any insert so no duplicate
        # row is created and edges/states attach to the canonical row.
        existing_rows = await db.execute(
            select(Character).where(Character.project_id == project_id)
        )
        existing = {c.name: c for c in existing_rows.scalars().all()}
        alias_of = _alias_to_canonical(existing)
        def canonical(name):
            return name if name in existing else alias_of.get(name, name)
        state_sources = {(canonical(name), idx): value for (name, idx), value in state_sources.items()}
        location_sources = {(canonical(name), loc, idx): value for (name, loc, idx), value in location_sources.items()}
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

        # Only explicit author edits replace an established card. Automatic
        # or unmarked historical profiles initialize only new cards. A
        # deliberate author clear ({}) is meaningful and must stay empty.
        if char_names and char_profiles and not preserve_existing_profiles:
            all_rows = await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.name.in_(char_names),
                )
            )
            for c in all_rows.scalars().all():
                new_profile = projected_profile(c.profile_json, char_profiles.get(c.name),
                    char_profile_origins.get(c.name))
                if new_profile is not None and c.profile_json != new_profile:
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

        # Carry explicit author metadata and stable IDs through type changes;
        # the graph remains authoritative and PG volume history stays intact.
        from app.services.relationship_projection import relationship_projection_rows, upsert_relationship_projection
        rel_rows = relationship_projection_rows(project_id, relationship_records,
            {name: char.id for name, char in by_name.items()}, canonical)
        if rel_rows:
            created_rels += await upsert_relationship_projection(db, rel_rows)

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
            if strict:
                raise
            logger.exception("relationships_deletion_sync_failed")

        # Preserve separate registrations, even if the difference is just a
        # negation or condition. Only explicit author edits replace rules.
        if world_rules:
            rules_q = await db.execute(
                select(WorldRule).where(WorldRule.project_id == project_id)
            )
            existing_rule_objs = list(rules_q.scalars().all())
            rule_inserts, _ = _plan_world_rule_writes(
                [(r.category, r.rule_text) for r in existing_rule_objs],
                list(world_rules),
            )
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
            created_locs += await upsert_projection_rows(db, Location, loc_rows, constraint="uq_locations_project_name")

        # Organizations: bulk insert w/ ON CONFLICT DO NOTHING.
        org_rows = [
            {"project_id": project_id, "name": name}
            for name in organizations
        ]
        if org_rows:
            created_orgs += await upsert_projection_rows(db, Organization, org_rows, constraint="uq_organizations_project_name")

        # Foreshadows: upsert by primary key id.
        # source='neo4j' marks rows as Neo4j-materialized so the deletion
        # sync below can tell them apart from PG-only rows (e.g. the
        # foreshadow_lifecycle outline pipeline, which never writes Neo4j).
        if foreshadows:
            fs_rows = []
            for f in foreshadows:
                fs_rows.append({"project_id": project_id, "source": "neo4j", **f})
            from app.services.foreshadow_projection import project_foreshadow_rows
            upserted_foreshadows += await project_foreshadow_rows(db, project_id, fs_rows)

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
        for start in range(0, len(stale_ids), 500):
            await db.execute(delete(Foreshadow).where(
                Foreshadow.project_id == project_id,
                Foreshadow.id.in_(stale_ids[start:start+500]),
            ))

        # MEMBER_OF: bulk insert projection rows.
        if memberships:
            # Refresh lookup maps (characters + organizations).
            mem_char_rows = await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.name.in_(sorted({c for (c, _, _, _) in memberships})),
                )
            )
            mem_char_by_name = {c.name: c for c in mem_char_rows.scalars().all()}
            mem_org_rows = await db.execute(
                select(Organization).where(
                    Organization.project_id == project_id,
                    Organization.name.in_(sorted({o for (_, o, _, _) in memberships})),
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

                created_memberships += await upsert_projection_rows(db, CharacterOrganization, mem_rows,
                    constraint="uq_character_organizations_key")

        # These tables are Neo4j read projections: remove keys absent from
        # the current graph snapshot, rather than leaving deleted facts forever.
        projection_chars = (await db.execute(select(Character.id, Character.name).where(
            Character.project_id == project_id))).all()
        projection_ids = {name: str(cid) for cid, name in projection_chars}
        projection_locs = (await db.execute(select(Location.id, Location.name).where(
            Location.project_id == project_id))).all()
        location_ids = {name: str(lid) for lid, name in projection_locs}
        keep_states = {(projection_ids[name], start) for name, start, end, state in cstates
                       if name in projection_ids}
        keep_locations = {(projection_ids[name], location_ids[loc], start)
                          for name, loc, start, end in at_locs
                          if name in projection_ids and loc in location_ids}
        for model, columns, keep in (
            (CharacterState, (CharacterState.character_id, CharacterState.chapter_start), keep_states),
            (CharacterLocation, (CharacterLocation.character_id, CharacterLocation.location_id,
                                 CharacterLocation.chapter_start), keep_locations),
        ):
            existing = (await db.execute(select(model.id, *columns).where(model.project_id == project_id))).all()
            doomed = [row[0] for row in existing
                      if tuple(str(value) if isinstance(value, UUID) else value
                               for value in row[1:]) not in keep]
            for start in range(0, len(doomed), 500):
                await db.execute(delete(model).where(model.id.in_(doomed[start:start+500])))

        # AT_LOCATION: bulk insert projection rows.
        if at_locs:
            # Refresh lookup maps (characters + locations).
            char_rows = await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.name.in_(sorted({c for (c, _, _, _) in at_locs})),
                )
            )
            char_by_name = {c.name: c for c in char_rows.scalars().all()}
            loc_rows_db = await db.execute(
                select(Location).where(
                    Location.project_id == project_id,
                    Location.name.in_(sorted({l for (_, l, _, _) in at_locs})),
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
                        "source_chapter_id": location_sources.get((cname, lname, cs), (None, None))[0],
                        "source_hash": location_sources.get((cname, lname, cs), (None, None))[1],
                        "chapter_start": int(cs),
                        "chapter_end": int(ce) if ce is not None else None,
                    }
                )
            if atloc_rows:
                created_atlocs += await upsert_projection_rows(db, CharacterLocation, atloc_rows,
                    constraint="uq_character_locations_key",
                    update_columns=("chapter_end", "source_chapter_id", "source_hash"))

        # HAS_STATE: bulk insert projection rows.
        if cstates:
            import uuid as _uuid

            cs_char_rows = await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.name.in_(sorted({c for (c, _, _, _) in cstates})),
                )
            )
            cs_by_name = {c.name: c for c in cs_char_rows.scalars().all()}

            cs_rows = []
            skipped_cstates_unchanged = 0
            for (cname, cs, ce, status_str) in cstates:
                c = cs_by_name.get(cname)
                if not c:
                    skipped_cstates_missing_character += 1
                    continue
                parsed_state = json.loads(status_str)
                if not isinstance(parsed_state, dict):
                    raise ValueError("Graph character state is not an object")
                cs_rows.append(
                    {
                        "id": str(_uuid.uuid4()),
                        "project_id": project_id,
                        "character_id": str(c.id),
                        "chapter_start": int(cs),
                        "chapter_end": int(ce) if ce is not None else None,
                        "status_json": parsed_state,
                        "source_chapter_id": state_sources.get((cname, cs), (None, None))[0],
                        "source_hash": state_sources.get((cname, cs), (None, None, None))[1],
                        # 每角色每章可以有多条状态观测,唯一键按"来源 + 内容摘要"去重
                        # (同源重放幂等,同章两条不同观测共存)。见迁移 a1001959。
                        "status_digest": hashlib.sha256(json.dumps(
                            parsed_state, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':')).encode()).hexdigest(),
                        "source_mode": state_sources.get((cname, cs), (None, None, None))[2],
                    }
                )

            if cs_rows:
                created_cstates += await upsert_projection_rows(db, CharacterState, cs_rows,
                    constraint="uq_character_states_source",
                    update_columns=("chapter_end", "status_json", "source_mode", "source_chapter_id", "source_hash"))

        # Caller commits cleanup journal and projection together before acknowledgement.
        logger.info(
            "entity_pg_materialize staged (project=%s ch=%d caller=%s chars=%d/%d rels=%d/%d rules=%d+%du/%d locs=%d/%d orgs=%d/%d member_of=%d/%d foreshadows=%d/%d atlocs=%d/%d cstates=%d/%d)",
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
        if strict:
            raise
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


async def _resolve_chapter(project_id: str, chapter_idx: int, chapter_id: str | None):
    from app.db.session import async_session_factory
    from app.services.entity_sources import resolve_entity_source
    async with async_session_factory() as db:
        return await resolve_entity_source(db, project_id, chapter_idx, chapter_id)


async def _extract_chapter_async(project_id: str, chapter_idx: int, caller: str,
                                 chapter_id: str | None = None, delivery_id: str | None = None) -> dict[str, Any]:
    from app.db.session import async_session_factory, engine
    from app.db.neo4j import init_neo4j, close_neo4j
    from app.db import neo4j as neo4j_mod
    from app.services.entity_timeline import EntityTimelineService
    from app.services.entity_projection import run_entity_projection, ensure_record

    source = await _resolve_chapter(project_id, chapter_idx, chapter_id)
    if source is None:
        return {'status': 'skipped', 'reason': 'unverified_source',
                'project_id': project_id, 'chapter_idx': chapter_idx}
    await ensure_record(async_session_factory, source)
    await init_neo4j()
    try:
        driver = neo4j_mod._driver
        if driver is None:
            raise RuntimeError('Neo4j unavailable')
        await EntityTimelineService(driver, strict_writes=True).initialize_graph(project_id)
        result = await run_entity_projection(
            source=source, sessions=async_session_factory, engine=engine, driver=driver,
            caller=caller, materialize=_materialize_entities_to_postgres,
            memory_cards=_upsert_memory_cards,
            delivery_id=delivery_id,
        )
        return {**result, 'project_id': project_id, 'chapter_idx': chapter_idx, 'caller': caller}
    finally:
        await close_neo4j()


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
            delivery_id=getattr(self.request, 'id', None),
        )
    )


@celery_app.task(name="entities.replay_pending")
def replay_pending_entity_extractions():
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.entity_projection import replay_pending_extractions
    return _run_async_safe(replay_pending_extractions(async_session_factory))


@celery_app.task(name="entities.drain_source_outbox")
def drain_entity_source_outbox():
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.entity_outbox import drain_entity_source_outbox as drain
    return _run_async_safe(drain(async_session_factory))


async def _replay_relationship_writes_async():
    from neo4j import AsyncGraphDatabase
    from app.config import settings
    from app.db.session import async_session_factory
    from app.services.relationship_writes import replay_relationship_writes
    async with AsyncGraphDatabase.driver(settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)) as driver:
        return await replay_relationship_writes(driver, sessions=async_session_factory)


@celery_app.task(name="entities.replay_relationship_writes")
def replay_relationship_author_writes():
    from app.tasks import _run_async_safe
    return _run_async_safe(_replay_relationship_writes_async())


async def _replay_entity_invalidations_async():
    from neo4j import AsyncGraphDatabase
    from app.config import settings
    from app.db.session import async_session_factory, engine
    from app.services.entity_invalidation import replay_entity_invalidations
    async with AsyncGraphDatabase.driver(settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)) as driver:
        return await replay_entity_invalidations(sessions=async_session_factory, engine=engine,
            driver=driver, materialize=_materialize_entities_to_postgres)


@celery_app.task(name="entities.replay_invalidations")
def replay_entity_invalidations():
    from app.tasks import _run_async_safe
    return _run_async_safe(_replay_entity_invalidations_async())


async def _extract_cognition_async(project_id, chapter_id, source_hash, *, delivery_id=None, cycle_id=None):
    from app.db.session import async_session_factory, engine
    from app.models.cognition import ChapterCognition as Record
    from app.services.cognition_extraction import run_cognition_extraction
    from sqlalchemy import update
    from app.services.cognition_reader import resolve_cognition_source
    from app.services.entity_projection import now
    # Classification and obsolescence share the source row lock. A restore
    # cannot race between our check and marking an old record obsolete.
    async with async_session_factory() as db:
        source = await resolve_cognition_source(db, project_id, chapter_id)
        if source is None or source.source_hash != source_hash:
            await db.execute(update(Record).where(Record.project_id == project_id,
                Record.chapter_id == chapter_id, Record.source_hash == source_hash,
                Record.cycle_id == UUID(str(cycle_id)) if cycle_id else Record.cycle_id == Record.id)
                .values(status='obsolete', attempt_token=None, updated_at=now()))
            await db.commit()
            return {'status': 'obsolete'}
        await db.commit()
    return await run_cognition_extraction(source, sessions=async_session_factory, engine=engine,
                                         delivery_id=delivery_id, recovery=True, cycle_id=cycle_id)


@celery_app.task(name='cognition.extract_chapter', bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def extract_chapter_cognition(self, project_id, chapter_id, source_hash, cycle_id=None):
    from app.tasks import _run_async_safe
    return _run_async_safe(_extract_cognition_async(project_id, chapter_id, source_hash,
        delivery_id=getattr(self.request, 'id', None), cycle_id=cycle_id))


@celery_app.task(name='cognition.replay_pending')
def replay_cognition():
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.cognition_extraction import replay_pending_cognition
    return _run_async_safe(replay_pending_cognition(async_session_factory))


@celery_app.task(name='cognition.cleanup_deleted_projects')
def cleanup_cognition_indexes():
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.cognition_index_maintenance import discover_index_cleanup
    result = _run_async_safe(discover_index_cleanup(async_session_factory))
    projects = result.pop('projects')
    dispatched = failures = 0
    for project_id, collection_names in projects.items():
        try:
            cleanup_project_cognition_indexes.apply_async(args=[project_id, None, collection_names], expires=3600)
            dispatched += 1
        except Exception as exc:
            failures += 1
            logger.warning('Cognition cleanup dispatch failed (%s)', type(exc).__name__)
    return {**result, 'dispatched': dispatched, 'dispatch_failures': failures}


@celery_app.task(name='cognition.cleanup_project_indexes')
def cleanup_project_cognition_indexes(project_id, cursor=None, collection_names=None):
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.cognition_index_maintenance import cleanup_project_indexes
    result = _run_async_safe(cleanup_project_indexes(project_id, async_session_factory, cursor=cursor,
        collection_names=collection_names))
    if result['status'] == 'partial':
        # The previous batch has released its DB/index resources. Every
        # continuation revalidates current sources rather than trusting this
        # cursor as proof of ownership or freshness.
        cleanup_project_cognition_indexes.apply_async(args=[project_id, result['cursor'], collection_names], expires=3600)
    return result


@celery_app.task(name='entities.replay_outline_settings')
def replay_outline_settings():
    from app.tasks import _run_async_safe
    from app.db.session import async_session_factory
    from app.services.outline_settings_delivery import replay_settings
    return _run_async_safe(replay_settings(async_session_factory))


async def _replay_author_profiles_async():
    from app.db import neo4j
    from app.services.character_profile_recovery import replay_author_profiles
    await neo4j.init_neo4j()
    if neo4j._driver is None:
        raise RuntimeError('graph_unavailable')
    return await replay_author_profiles(neo4j._driver)


@celery_app.task(name='entities.replay_author_profiles')
def replay_author_profiles():
    from app.tasks import _run_async_safe
    return _run_async_safe(_replay_author_profiles_async())


async def _replay_character_deletions_async():
    from app.db import neo4j
    from app.services.character_deletion import replay_character_deletions
    await neo4j.init_neo4j()
    if neo4j._driver is None:
        raise RuntimeError('graph_unavailable')
    return await replay_character_deletions(neo4j._driver)


@celery_app.task(name='entities.replay_character_deletions')
def replay_character_deletions():
    from app.tasks import _run_async_safe
    return _run_async_safe(_replay_character_deletions_async())
