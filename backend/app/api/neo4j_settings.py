"""Neo4j settings write endpoints (v1.9+).

Purpose
-------
The legacy settings endpoints in ``app.api.settings`` read/write Postgres.
In the recommended architecture, Neo4j is the source of truth and Postgres is
strictly a read-optimized projection.

This module provides *write* endpoints that:
1) write to Neo4j (source of truth)
2) immediately materialize into Postgres read models (best-effort)

Notes
-----
- These endpoints intentionally return minimal payloads to avoid coupling the
  response shape to Postgres row IDs (which are projection artifacts).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from neo4j import AsyncDriver
from pydantic import BaseModel, Field

from app.db.neo4j import get_neo4j
from app.tasks.entity_tasks import _materialize_entities_to_postgres


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/neo4j-settings",
    tags=["settings"],
)


class Neo4jCharacterUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    profile_json: dict[str, Any] | None = None


class Neo4jCharacterDeleteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.post("/characters", status_code=202)
async def upsert_character(
    project_id: str,
    body: Neo4jCharacterUpsertRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Upsert a Character node in Neo4j, then materialize to Postgres."""
    try:
        async with neo4j.session() as session:
            result = await session.run(
                "MERGE (c:Character {project_id: $pid, name: $name}) "
                "ON CREATE SET c.id = $id "
                "SET c.profile_json = $profile "
                "RETURN c.id AS id",
                pid=str(project_id),
                name=str(body.name).strip(),
                id=str(uuid.uuid4()),
                profile=json.dumps(body.profile_json or {}, ensure_ascii=False),
            )
            await result.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    # Best-effort projection
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.characters.upsert",
    )
    return {"status": "accepted", "entity": "character", "name": body.name}


async def _delete_character_rows_from_postgres(*, project_id: str, name: str) -> None:
    """Best-effort: remove the projected characters row and name-keyed satellites.

    ``_materialize_entities_to_postgres`` only deletion-syncs relationships —
    a Character deleted in Neo4j would linger in PG forever. The characters.id
    foreign keys (character_states / character_locations /
    character_organizations / relationships) are ON DELETE CASCADE, so
    deleting the row covers those; character_appearances is keyed by
    character_name (no FK) and is removed explicitly.
    """
    from sqlalchemy import delete

    from app.db.session import async_session_factory
    from app.models.project import Character, CharacterAppearance

    try:
        async with async_session_factory() as db:
            await db.execute(
                delete(Character).where(
                    Character.project_id == project_id,
                    Character.name == name,
                )
            )
            await db.execute(
                delete(CharacterAppearance).where(
                    CharacterAppearance.project_id == project_id,
                    CharacterAppearance.character_name == name,
                )
            )
            await db.commit()
    except Exception:
        logger.exception("character_pg_cleanup_failed")


@router.delete("/characters", status_code=202)
async def delete_character(
    project_id: str,
    name: str | None = None,
    body: Neo4jCharacterDeleteRequest | None = None,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Delete a Character node in Neo4j, then materialize to Postgres.

    Accepts ``name`` either as a query param or as a JSON body
    (DELETE-with-body support varies across HTTP clients). HAS_STATE state
    nodes are separate nodes, so they are deleted explicitly; DETACH DELETE
    on the character covers its edges (RELATES_TO / AT_LOCATION / MEMBER_OF).
    """
    cname = str(body.name if body else name or "").strip()
    if not cname:
        raise HTTPException(
            status_code=422,
            detail="name is required (query param or JSON body)",
        )

    try:
        async with neo4j.session() as session:
            r1 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $name})-[:HAS_STATE]->(s) "
                "DETACH DELETE s "
                "RETURN count(s) AS deleted_states",
                pid=str(project_id),
                name=cname,
            )
            await r1.consume()
            r2 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $name}) "
                "DETACH DELETE c "
                "RETURN count(c) AS deleted",
                pid=str(project_id),
                name=cname,
            )
            record = await r2.single()
            deleted = int(record["deleted"]) if record else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    if deleted == 0:
        raise HTTPException(status_code=404, detail="character_not_found")

    await _delete_character_rows_from_postgres(project_id=str(project_id), name=cname)
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.characters.delete",
    )
    return {
        "status": "accepted",
        "entity": "character",
        "name": cname,
        "deleted": deleted,
    }


class Neo4jWorldRuleCreateRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    rule_text: str = Field(..., min_length=1)


class Neo4jWorldRuleUpdateRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    old_text: str = Field(..., min_length=1)
    new_text: str = Field(..., min_length=1)
    new_category: str | None = Field(None, min_length=1, max_length=100)


class Neo4jWorldRuleDeleteRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    text: str = Field(..., min_length=1)


@router.post("/world-rules", status_code=202)
async def create_world_rule(
    project_id: str,
    body: Neo4jWorldRuleCreateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Create a WorldRule node in Neo4j, then materialize to Postgres."""
    rid = str(uuid.uuid4())
    try:
        async with neo4j.session() as session:
            # Use MERGE to align with Neo4j uniqueness constraint and keep write idempotent.
            result = await session.run(
                "MERGE (w:WorldRule {project_id: $pid, category: $cat, text: $txt}) "
                "ON CREATE SET w.id = $id "
                "RETURN w.id AS id",
                id=rid,
                pid=str(project_id),
                cat=str(body.category).strip(),
                txt=str(body.rule_text).strip(),
            )
            await result.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.world_rules.create",
    )
    return {"status": "accepted", "entity": "world_rule", "id": rid}


async def _update_world_rule_row_in_postgres(
    *,
    project_id: str,
    category: str,
    old_text: str,
    new_category: str,
    new_text: str,
) -> None:
    """Best-effort: converge the projected world_rules row after a Neo4j edit.

    ``_materialize_entities_to_postgres`` inserts/updates world rules but
    never deletes, and its near-duplicate detection (_plan_world_rule_writes)
    only treats >80%-overlap same-category text as an update — a bigger edit
    would insert the new text and leave the stale old row behind. Rewrite the
    old row in place (preserving the metadata_json cascade sidecar); if a row
    with the target (category, text) already exists, drop the old row instead.
    """
    from sqlalchemy import delete, select

    from app.db.session import async_session_factory
    from app.models.project import WorldRule

    try:
        async with async_session_factory() as db:
            old_q = await db.execute(
                select(WorldRule).where(
                    WorldRule.project_id == project_id,
                    WorldRule.category == category,
                    WorldRule.rule_text == old_text,
                )
            )
            old_row = old_q.scalars().first()
            if old_row is None:
                # Nothing stale in PG; materialize will insert the new text.
                return
            target_q = await db.execute(
                select(WorldRule).where(
                    WorldRule.project_id == project_id,
                    WorldRule.category == new_category,
                    WorldRule.rule_text == new_text,
                    WorldRule.id != old_row.id,
                )
            )
            if target_q.scalars().first() is not None:
                await db.execute(delete(WorldRule).where(WorldRule.id == old_row.id))
            else:
                old_row.category = new_category
                old_row.rule_text = new_text
            await db.commit()
    except Exception:
        logger.exception("world_rule_pg_update_failed")


async def _delete_world_rule_row_from_postgres(
    *, project_id: str, category: str, rule_text: str
) -> None:
    """Best-effort: remove the projected world_rules row.

    ``_materialize_entities_to_postgres`` has no deletion sync for world
    rules, so a Neo4j delete would otherwise leave the stale PG row behind.
    """
    from sqlalchemy import delete

    from app.db.session import async_session_factory
    from app.models.project import WorldRule

    try:
        async with async_session_factory() as db:
            await db.execute(
                delete(WorldRule).where(
                    WorldRule.project_id == project_id,
                    WorldRule.category == category,
                    WorldRule.rule_text == rule_text,
                )
            )
            await db.commit()
    except Exception:
        logger.exception("world_rule_pg_cleanup_failed")


@router.put("/world-rules", status_code=202)
async def update_world_rule(
    project_id: str,
    body: Neo4jWorldRuleUpdateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Update a WorldRule node's text/category in Neo4j, then materialize.

    The node is keyed by (project_id, category, text) — see create_world_rule —
    so "update" matches the existing node by (category, old_text) and rewrites
    its properties in place (preserving id and any edges) rather than MERGE-ing
    a fresh node. If a node with the target (category, text) already exists,
    the edit collapses into it: the old node is deleted instead of creating a
    duplicate (consistent with the MERGE-keyed uniqueness of the POST route).
    """
    cat = str(body.category).strip()
    old_txt = str(body.old_text).strip()
    new_txt = str(body.new_text).strip()
    new_cat = str(body.new_category).strip() if body.new_category else cat
    key_changed = (new_cat, new_txt) != (cat, old_txt)

    deduplicated = False
    try:
        async with neo4j.session() as session:
            target_exists = False
            if key_changed:
                r0 = await session.run(
                    "MATCH (w:WorldRule {project_id: $pid, category: $new_cat, text: $new_txt}) "
                    "RETURN count(w) AS existing",
                    pid=str(project_id),
                    new_cat=new_cat,
                    new_txt=new_txt,
                )
                rec = await r0.single()
                target_exists = bool(rec and int(rec["existing"]) > 0)
            if target_exists:
                # Target node already exists: delete the old node so the edit
                # dedupes into it instead of SET-ing a second identical node.
                r1 = await session.run(
                    "MATCH (w:WorldRule {project_id: $pid, category: $cat, text: $old_txt}) "
                    "DETACH DELETE w "
                    "RETURN count(w) AS matched",
                    pid=str(project_id),
                    cat=cat,
                    old_txt=old_txt,
                )
                deduplicated = True
            else:
                r1 = await session.run(
                    "MATCH (w:WorldRule {project_id: $pid, category: $cat, text: $old_txt}) "
                    "SET w.category = $new_cat, w.text = $new_txt "
                    "RETURN count(w) AS matched",
                    pid=str(project_id),
                    cat=cat,
                    old_txt=old_txt,
                    new_cat=new_cat,
                    new_txt=new_txt,
                )
            record = await r1.single()
            matched = int(record["matched"]) if record else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    if matched == 0:
        raise HTTPException(status_code=404, detail="world_rule_not_found")

    await _update_world_rule_row_in_postgres(
        project_id=str(project_id),
        category=cat,
        old_text=old_txt,
        new_category=new_cat,
        new_text=new_txt,
    )
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.world_rules.update",
    )
    return {
        "status": "accepted",
        "entity": "world_rule",
        "category": new_cat,
        "text": new_txt,
        "deduplicated": deduplicated,
    }


@router.delete("/world-rules", status_code=202)
async def delete_world_rule(
    project_id: str,
    category: str | None = None,
    text: str | None = None,
    body: Neo4jWorldRuleDeleteRequest | None = None,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Delete a WorldRule node in Neo4j, then materialize to Postgres.

    Accepts (category, text) either as query params or as a JSON body
    (DELETE-with-body support varies across HTTP clients).
    """
    cat = str(body.category if body else category or "").strip()
    txt = str(body.text if body else text or "").strip()
    if not (cat and txt):
        raise HTTPException(
            status_code=422,
            detail="category and text are required (query params or JSON body)",
        )

    try:
        async with neo4j.session() as session:
            result = await session.run(
                "MATCH (w:WorldRule {project_id: $pid, category: $cat, text: $txt}) "
                "DETACH DELETE w "
                "RETURN count(w) AS deleted",
                pid=str(project_id),
                cat=cat,
                txt=txt,
            )
            record = await result.single()
            deleted = int(record["deleted"]) if record else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    if deleted == 0:
        raise HTTPException(status_code=404, detail="world_rule_not_found")

    await _delete_world_rule_row_from_postgres(
        project_id=str(project_id), category=cat, rule_text=txt
    )
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.world_rules.delete",
    )
    return {
        "status": "accepted",
        "entity": "world_rule",
        "category": cat,
        "text": txt,
        "deleted": deleted,
    }


class Neo4jRelationshipCreateRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    rel_type: str = Field(..., min_length=1, max_length=100)
    chapter_start: int = Field(default=0, ge=0)


class Neo4jRelationshipUpdateRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    rel_type: str = Field(..., min_length=1, max_length=100)
    new_rel_type: str | None = Field(None, min_length=1, max_length=100)
    label: str | None = Field(None, max_length=200)
    note: str | None = None
    sentiment: str | None = Field(None, max_length=50)


class Neo4jRelationshipDeleteRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    rel_type: str = Field(..., min_length=1, max_length=100)


class Neo4jSetLocationRequest(BaseModel):
    character: str = Field(..., min_length=1, max_length=200)
    location: str = Field(..., min_length=1, max_length=200)
    chapter_start: int = Field(default=0, ge=0)


class Neo4jSetMembershipRequest(BaseModel):
    character: str = Field(..., min_length=1, max_length=200)
    organization: str = Field(..., min_length=1, max_length=200)
    chapter_start: int = Field(default=0, ge=0)


class Neo4jForeshadowUpsertRequest(BaseModel):
    id: str | None = Field(None, description="Optional stable id; if omitted a new UUID is generated")
    type: str = Field(..., min_length=1, max_length=20)
    description: str = Field(..., min_length=1)
    planted_chapter: int = Field(default=0, ge=0)
    resolve_conditions: list[str] = Field(default_factory=list)
    resolution_blueprint: dict[str, Any] | None = None
    narrative_proximity: float = Field(default=0.0, ge=0.0, le=1.0)
    status: str = Field(default="planted", min_length=1, max_length=20)
    resolved_chapter: int | None = None


@router.post("/relationships", status_code=202)
async def create_relationship(
    project_id: str,
    body: Neo4jRelationshipCreateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Create a RELATES_TO edge in Neo4j, then materialize to Postgres."""
    try:
        async with neo4j.session() as session:
            # Ensure both characters exist.
            r1 = await session.run(
                "MERGE (a:Character {project_id: $pid, name: $src}) "
                "ON CREATE SET a.id = $aid",
                pid=str(project_id),
                src=str(body.source).strip(),
                aid=str(uuid.uuid4()),
            )
            await r1.consume()
            r2 = await session.run(
                "MERGE (b:Character {project_id: $pid, name: $tgt}) "
                "ON CREATE SET b.id = $bid",
                pid=str(project_id),
                tgt=str(body.target).strip(),
                bid=str(uuid.uuid4()),
            )
            await r2.consume()
            # Use MERGE to keep this endpoint idempotent under retries.
            # We also persist identifying fields on the relationship to support
            # Neo4j uniqueness constraints.
            # Store canonical type on r.type (materialize expects r.type), and
            # preserve original user-provided type for audit/debug.
            from app.services.rel_type import canonicalize_rel_type

            raw_type = str(body.rel_type).strip()
            rtype = canonicalize_rel_type(raw_type)

            r3 = await session.run(
                "MATCH (a:Character {project_id: $pid, name: $src}), "
                "      (b:Character {project_id: $pid, name: $tgt}) "
                "MERGE (a)-[r:RELATES_TO {project_id: $pid, source_name: $src, target_name: $tgt, type: $rtype, chapter_start: $cs}]->(b) "
                "ON CREATE SET r.chapter_end = null, r.raw_type = $raw_type "
                "SET r.raw_type = coalesce(r.raw_type, $raw_type)",
                pid=str(project_id),
                src=str(body.source).strip(),
                tgt=str(body.target).strip(),
                rtype=rtype,
                raw_type=raw_type,
                cs=int(body.chapter_start),
            )
            await r3.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=int(body.chapter_start),
        caller="api.neo4j_settings.relationships.create",
    )
    return {
        "status": "accepted",
        "entity": "relationship",
        "source": body.source,
        "target": body.target,
        "rel_type": body.rel_type,
    }


@router.put("/relationships", status_code=202)
async def update_relationship(
    project_id: str,
    body: Neo4jRelationshipUpdateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Update a RELATES_TO edge's properties in Neo4j, then materialize.

    The edge is matched by (project_id, source, target, rel_type). Note the
    semantic relationship type lives in the ``r.type`` property — the Neo4j
    relationship TYPE is always RELATES_TO (see create_relationship and
    _materialize_entities_to_postgres) — so renaming via ``new_rel_type`` is
    a plain property SET: no delete+recreate dance is needed and all other
    edge properties are preserved automatically.

    Extraction-written edges may carry a non-canonical ``r.type`` while the
    Postgres projection exposes the canonical token, so we match either.
    """
    from app.services.rel_type import canonicalize_rel_type

    src = str(body.source).strip()
    tgt = str(body.target).strip()
    raw_rtype = str(body.rel_type).strip()
    rtype = canonicalize_rel_type(raw_rtype)

    provided = body.model_dump(exclude_unset=True)
    set_clauses: list[str] = []
    params: dict[str, Any] = {
        "pid": str(project_id),
        "src": src,
        "tgt": tgt,
        "rtype": rtype,
        "raw_rtype": raw_rtype,
    }
    if body.new_rel_type is not None:
        new_raw = str(body.new_rel_type).strip()
        params["new_rtype"] = canonicalize_rel_type(new_raw)
        params["new_raw_type"] = new_raw
        set_clauses.append("r.type = $new_rtype")
        set_clauses.append("r.raw_type = $new_raw_type")
    for field in ("label", "note", "sentiment"):
        if field in provided:
            params[field] = provided[field]
            set_clauses.append(f"r.{field} = ${field}")

    set_cypher = ("SET " + ", ".join(set_clauses) + " ") if set_clauses else ""
    try:
        async with neo4j.session() as session:
            result = await session.run(
                "MATCH (a:Character {project_id: $pid, name: $src})"
                "-[r:RELATES_TO]->"
                "(b:Character {project_id: $pid, name: $tgt}) "
                "WHERE r.type IN [$rtype, $raw_rtype] "
                + set_cypher
                + "RETURN count(r) AS matched",
                **params,
            )
            record = await result.single()
            matched = int(record["matched"]) if record else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    if matched == 0:
        raise HTTPException(status_code=404, detail="relationship_not_found")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.relationships.update",
    )
    return {
        "status": "accepted",
        "entity": "relationship",
        "source": src,
        "target": tgt,
        "rel_type": params.get("new_rtype", rtype),
    }


@router.delete("/relationships", status_code=202)
async def delete_relationship(
    project_id: str,
    source: str | None = None,
    target: str | None = None,
    rel_type: str | None = None,
    body: Neo4jRelationshipDeleteRequest | None = None,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Delete a RELATES_TO edge in Neo4j, then materialize to Postgres.

    Accepts (source, target, rel_type) either as query params or as a JSON
    body (DELETE-with-body support varies across HTTP clients). Matching
    mirrors update_relationship: canonical or raw ``r.type`` both match.
    """
    from app.services.rel_type import canonicalize_rel_type

    src = str(body.source if body else source or "").strip()
    tgt = str(body.target if body else target or "").strip()
    raw_rtype = str(body.rel_type if body else rel_type or "").strip()
    if not (src and tgt and raw_rtype):
        raise HTTPException(
            status_code=422,
            detail="source, target and rel_type are required (query params or JSON body)",
        )
    rtype = canonicalize_rel_type(raw_rtype)

    try:
        async with neo4j.session() as session:
            result = await session.run(
                "MATCH (a:Character {project_id: $pid, name: $src})"
                "-[r:RELATES_TO]->"
                "(b:Character {project_id: $pid, name: $tgt}) "
                "WHERE r.type IN [$rtype, $raw_rtype] "
                "DELETE r "
                "RETURN count(r) AS deleted",
                pid=str(project_id),
                src=src,
                tgt=tgt,
                rtype=rtype,
                raw_rtype=raw_rtype,
            )
            record = await result.single()
            deleted = int(record["deleted"]) if record else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    if deleted == 0:
        raise HTTPException(status_code=404, detail="relationship_not_found")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.neo4j_settings.relationships.delete",
    )
    return {
        "status": "accepted",
        "entity": "relationship",
        "source": src,
        "target": tgt,
        "rel_type": rtype,
        "deleted": deleted,
    }


@router.post("/locations/set", status_code=202)
async def set_character_location(
    project_id: str,
    body: Neo4jSetLocationRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Set character location in Neo4j via AT_LOCATION, then materialize to Postgres."""
    cname = str(body.character).strip()
    lname = str(body.location).strip()
    cs = int(body.chapter_start)

    try:
        async with neo4j.session() as session:
            # Ensure nodes exist
            r1 = await session.run(
                "MERGE (c:Character {project_id: $pid, name: $cname}) "
                "ON CREATE SET c.id = $cid",
                pid=str(project_id),
                cname=cname,
                cid=str(uuid.uuid4()),
            )
            await r1.consume()
            r2 = await session.run(
                "MERGE (l:Location {project_id: $pid, name: $lname}) "
                "ON CREATE SET l.id = $lid",
                pid=str(project_id),
                lname=lname,
                lid=str(uuid.uuid4()),
            )
            await r2.consume()

            # Close previous open AT_LOCATION
            r3 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $cname})-[r:AT_LOCATION]->(:Location) "
                "WHERE r.chapter_end IS NULL "
                "SET r.chapter_end = $end",
                pid=str(project_id),
                cname=cname,
                end=cs - 1,
            )
            await r3.consume()

            # Open (idempotent) AT_LOCATION
            r4 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $cname}), (l:Location {project_id: $pid, name: $lname}) "
                "MERGE (c)-[r:AT_LOCATION {project_id: $pid, character_name: $cname, chapter_start: $cs}]->(l) "
                "ON CREATE SET r.chapter_end = null "
                "SET r.location_name = $lname",
                pid=str(project_id),
                cname=cname,
                lname=lname,
                cs=cs,
            )
            await r4.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=cs,
        caller="api.neo4j_settings.locations.set",
    )
    return {
        "status": "accepted",
        "entity": "at_location",
        "character": cname,
        "location": lname,
        "chapter_start": cs,
    }


@router.post("/organizations/set-membership", status_code=202)
async def set_character_membership(
    project_id: str,
    body: Neo4jSetMembershipRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Set character membership in Neo4j via MEMBER_OF.

    NOTE: We still call materialize as best-effort; Postgres is a projection.
    """
    cname = str(body.character).strip()
    oname = str(body.organization).strip()
    cs = int(body.chapter_start)

    try:
        async with neo4j.session() as session:
            # Ensure nodes exist
            r1 = await session.run(
                "MERGE (c:Character {project_id: $pid, name: $cname}) "
                "ON CREATE SET c.id = $cid",
                pid=str(project_id),
                cname=cname,
                cid=str(uuid.uuid4()),
            )
            await r1.consume()
            r2 = await session.run(
                "MERGE (o:Organization {project_id: $pid, name: $oname}) "
                "ON CREATE SET o.id = $oid",
                pid=str(project_id),
                oname=oname,
                oid=str(uuid.uuid4()),
            )
            await r2.consume()

            # Close previous open memberships (best-effort)
            r3 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $cname})-[r:MEMBER_OF]->(:Organization) "
                "WHERE r.chapter_end IS NULL "
                "SET r.chapter_end = $end",
                pid=str(project_id),
                cname=cname,
                end=cs - 1,
            )
            await r3.consume()

            # Open (idempotent) MEMBER_OF
            r4 = await session.run(
                "MATCH (c:Character {project_id: $pid, name: $cname}), (o:Organization {project_id: $pid, name: $oname}) "
                "MERGE (c)-[r:MEMBER_OF {project_id: $pid, character_name: $cname, org_name: $oname, chapter_start: $cs}]->(o) "
                "ON CREATE SET r.chapter_end = null",
                pid=str(project_id),
                cname=cname,
                oname=oname,
                cs=cs,
            )
            await r4.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=cs,
        caller="api.neo4j_settings.organizations.set_membership",
    )
    return {
        "status": "accepted",
        "entity": "member_of",
        "character": cname,
        "organization": oname,
        "chapter_start": cs,
    }


@router.post("/foreshadows/set", status_code=202)
async def upsert_foreshadow(
    project_id: str,
    body: Neo4jForeshadowUpsertRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
) -> dict[str, Any]:
    """Upsert a Foreshadow node in Neo4j, then materialize to Postgres (best-effort)."""
    # Postgres foreshadows.id is UUID, so the Neo4j Foreshadow.id must be a UUID string.
    # Accept caller-provided UUID, otherwise generate one.
    fid: str
    if body.id:
        try:
            fid = str(uuid.UUID(str(body.id).strip()))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_foreshadow_id: must be UUID")
    else:
        fid = str(uuid.uuid4())
    ftype = str(body.type).strip()
    desc = str(body.description).strip()
    planted = int(body.planted_chapter)
    status = str(body.status).strip()
    resolved_ch = int(body.resolved_chapter) if body.resolved_chapter is not None else None

    try:
        async with neo4j.session() as session:
            result = await session.run(
                "MERGE (f:Foreshadow {project_id: $pid, id: $id}) "
                "SET f.type = $type, "
                "    f.description = $desc, "
                "    f.planted_chapter = $planted, "
                "    f.resolve_conditions_json = $conds, "
                "    f.resolution_blueprint_json = $blueprint, "
                "    f.narrative_proximity = $prox, "
                "    f.status = $status, "
                "    f.resolved_chapter = $resolved "
                "RETURN f.id AS id",
                pid=str(project_id),
                id=fid,
                type=ftype,
                desc=desc,
                planted=planted,
                conds=json.dumps(list(body.resolve_conditions or []), ensure_ascii=False),
                blueprint=json.dumps(body.resolution_blueprint or {}, ensure_ascii=False),
                prox=float(body.narrative_proximity or 0.0),
                status=status,
                resolved=resolved_ch,
            )
            await result.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=planted,
        caller="api.neo4j_settings.foreshadows.upsert",
    )
    return {"status": "accepted", "entity": "foreshadow", "id": fid}

