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
            result = await session.run(
                "CREATE (w:WorldRule {id: $id, project_id: $pid, category: $cat, text: $txt})",
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
            r3 = await session.run(
                "MATCH (a:Character {project_id: $pid, name: $src}), "
                "      (b:Character {project_id: $pid, name: $tgt}) "
                "CREATE (a)-[:RELATES_TO {type: $rtype, chapter_start: $cs, chapter_end: null}]->(b)",
                pid=str(project_id),
                src=str(body.source).strip(),
                tgt=str(body.target).strip(),
                rtype=str(body.rel_type).strip(),
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

