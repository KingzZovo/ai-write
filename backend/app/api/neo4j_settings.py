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
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from neo4j import AsyncDriver
from pydantic import BaseModel, Field

from app.db.neo4j import get_neo4j
from app.tasks.entity_tasks import _materialize_entities_to_postgres


router = APIRouter(
    prefix="/api/projects/{project_id}/neo4j-settings",
    tags=["settings"],
)


class Neo4jCharacterUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    profile_json: dict[str, Any] | None = None


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


class Neo4jWorldRuleCreateRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    rule_text: str = Field(..., min_length=1)


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


class Neo4jRelationshipCreateRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    rel_type: str = Field(..., min_length=1, max_length=100)
    chapter_start: int = Field(default=0, ge=0)


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
    fid = str(body.id).strip() if body.id else str(uuid.uuid4())
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

