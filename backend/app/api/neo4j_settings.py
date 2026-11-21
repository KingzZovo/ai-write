"""Neo4j settings write endpoints (v1.9+).

Purpose
-------
Neo4j owns canonical settings and Postgres holds compatibility projections.
The world-rule list in ``app.api.settings`` and generation also read the graph
directly, so an incomplete projection cannot restore an obsolete rule.

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
from pydantic import BaseModel, Field, field_validator

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.character_project_guard import CharacterProjectError, character_graph_write_guard
from app.services.world_rule_material import WorldRuleSourceError
from app.services.world_rule_writes import write_world_rule
from app.tasks.entity_tasks import _materialize_entities_to_postgres


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/neo4j-settings",
    tags=["settings"],
)


class Neo4jCharacterUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    profile_json: dict[str, Any] = Field(..., description='Complete author profile; use an explicit empty object to clear')
    expected_profile_json: dict[str, Any] | None = Field(..., description='Original profile; null means create only')

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class Neo4jCharacterDeleteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    expected_profile_json: dict[str, Any]
    deletion_token: uuid.UUID


@router.post("/characters", status_code=202)
async def upsert_character(
    project_id: str,
    body: Neo4jCharacterUpsertRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Conditionally save an author card, then strictly synchronize its read model."""
    from app.services.character_profile_write import write_character_profile, CharacterProfileConflict
    try:
        async with character_graph_write_guard(db, project_id) as project_id:
            sync_token = await write_character_profile(neo4j, project_id, body.name, body.profile_json, body.expected_profile_json)
    except CharacterProjectError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None
    except CharacterProfileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail='人物保存未获确认，草稿已保留，请稍后核对重试。') from exc
    try:
        await _materialize_entities_to_postgres(project_id=str(project_id), chapter_idx=0,
            caller="api.neo4j_settings.characters.upsert", strict=True, graph_driver=neo4j)
        from app.services.character_profile_recovery import acknowledge_profiles
        await acknowledge_profiles(neo4j, project_id, [{'name': body.name, 'token': sync_token}])
    except Exception as exc:
        raise HTTPException(status_code=503, detail={'code': 'character_projection_pending',
            'message': '人物已写入，但同步未完成，后台将自动重试。草稿已保留。'}) from exc
    return {"status": "accepted", "entity": "character", "name": body.name}


@router.get('/characters/profile')
async def character_profile_status(project_id: str, name: str, neo4j: AsyncDriver = Depends(get_neo4j)):
    """Read canonical profile and recovery state without triggering writes."""
    name = name.strip()
    if not name or len(name) > 200:
        raise HTTPException(status_code=422, detail='人物姓名无效。')
    async with neo4j.session() as session:
        result = await session.run('MATCH (c:Character {project_id:$pid,name:$name}) '
            'RETURN c.profile_json AS profile_json,c.profile_sync_token IS NOT NULL AS pending',
            pid=str(project_id), name=name)
        row = await result.single()
    if row is None:
        raise HTTPException(status_code=404, detail='人物不存在或已被删除。')
    try:
        profile = json.loads(row['profile_json']) if row['profile_json'] is not None else {}
        if not isinstance(profile, dict): raise ValueError('invalid profile')
    except (ValueError, TypeError):
        raise HTTPException(status_code=409, detail='当前人物档案格式异常，请先核对。') from None
    return {'name': name, 'profile_json': profile, 'pending': bool(row['pending'])}


@router.delete("/characters", status_code=202)
async def delete_character(
    project_id: str,
    body: Neo4jCharacterDeleteRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.services.character_deletion import delete_character_graph
    from app.services.character_profile_write import CharacterProfileConflict
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail='人物姓名无效。')
    try:
        async with character_graph_write_guard(db, project_id) as project_id:
            await delete_character_graph(neo4j, project_id, name, body.expected_profile_json, body.deletion_token)
    except CharacterProjectError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None
    except CharacterProfileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail='删除未获确认，请保留当前页面并核对后重试。') from exc
    try:
        await _materialize_entities_to_postgres(project_id=str(project_id), chapter_idx=0,
            caller='api.neo4j_settings.characters.delete', strict=True, graph_driver=neo4j)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={'code': 'character_deletion_pending',
            'message': '人物已从图中删除，页面资料清理尚未完成，后台将自动重试。'}) from exc
    return {'status': 'accepted', 'entity': 'character', 'name': name, 'deleted': 1}


class Neo4jWorldRuleCreateRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    rule_text: str = Field(..., min_length=1)


class Neo4jWorldRuleUpdateRequest(BaseModel):
    category: str
    old_text: str = Field(..., min_length=1)
    new_text: str = Field(..., min_length=1)
    new_category: str | None = Field(None, min_length=1, max_length=100)
    expected_version: str | None = None


class Neo4jWorldRuleDeleteRequest(BaseModel):
    category: str
    text: str = Field(..., min_length=1)
    expected_version: str | None = None


async def _refresh_world_rule_projection(project_id, driver, caller):
    """Canonical graph commit is confirmed; this compatibility copy is optional."""
    try:
        await _materialize_entities_to_postgres(project_id=str(project_id), chapter_idx=0,
                                               caller=caller, graph_driver=driver)
    except Exception as exc:
        logger.warning('world_rule_projection_incomplete: %s', type(exc).__name__)


@router.post("/world-rules", status_code=202)
async def create_world_rule(
    project_id: str,
    body: Neo4jWorldRuleCreateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Register the current author setting, then refresh its read projection."""
    try:
        result = await write_world_rule(db, neo4j, project_id, 'create', category=body.category, text=body.rule_text)
    except WorldRuleSourceError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None

    await _refresh_world_rule_projection(project_id, neo4j, 'api.neo4j_settings.world_rules.create')
    return {"status": "accepted", "entity": "world_rule", **result}


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
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update a WorldRule node's text/category in Neo4j, then materialize.

    The node is keyed by (project_id, category, text) — see create_world_rule —
    so "update" matches the existing node by (category, old_text) and rewrites
    its properties in place (preserving id and any edges) rather than MERGE-ing
    a fresh node. If a node with the target (category, text) already exists,
    the edit collapses into it: the old node is deleted instead of creating a
    duplicate (consistent with the MERGE-keyed uniqueness of the POST route).
    """
    cat = body.category
    old_txt = body.old_text
    new_txt = str(body.new_text).strip()
    new_cat = str(body.new_category).strip() if body.new_category else cat
    try:
        result = await write_world_rule(db, neo4j, project_id, 'update', category=cat, text=old_txt,
                                       new_category=new_cat, new_text=new_txt, expected_version=body.expected_version)
    except WorldRuleSourceError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None

    await _update_world_rule_row_in_postgres(
        project_id=str(project_id),
        category=cat,
        old_text=old_txt,
        new_category=result['category'],
        new_text=result['text'],
    )
    await _refresh_world_rule_projection(project_id, neo4j, 'api.neo4j_settings.world_rules.update')
    return {
        "status": "accepted",
        "entity": "world_rule",
        **result,
    }


@router.delete("/world-rules", status_code=202)
async def delete_world_rule(
    project_id: str,
    category: str | None = None,
    text: str | None = None,
    expected_version: str | None = None,
    body: Neo4jWorldRuleDeleteRequest | None = None,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete a WorldRule node in Neo4j, then materialize to Postgres.

    Accepts (category, text, expected_version) as query params or a JSON body
    (DELETE-with-body support varies across HTTP clients).
    """
    cat = body.category if body else category
    txt = body.text if body else text
    if cat is None or txt is None or not txt.strip():
        raise HTTPException(
            status_code=422,
            detail="category and text are required (query params or JSON body)",
        )

    try:
        result = await write_world_rule(db, neo4j, project_id, 'delete', category=cat, text=txt,
                                       expected_version=body.expected_version if body else expected_version)
    except WorldRuleSourceError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None

    await _delete_world_rule_row_from_postgres(
        project_id=str(project_id), category=cat, rule_text=txt
    )
    await _refresh_world_rule_projection(project_id, neo4j, 'api.neo4j_settings.world_rules.delete')
    return {
        "status": "accepted",
        "entity": "world_rule",
        **result,
    }


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


# Relationship writes have their own versioned service and strict projection.
from app.api.relationship_writes import (  # noqa: E402
    router as relationship_router, Neo4jRelationshipCreateRequest,
    Neo4jRelationshipUpdateRequest, Neo4jRelationshipDeleteRequest,
    create_relationship, update_relationship, delete_relationship,
)
router.include_router(relationship_router)


@router.post("/locations/set", status_code=202)
async def set_character_location(
    project_id: str,
    body: Neo4jSetLocationRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set character location in Neo4j via AT_LOCATION, then materialize to Postgres."""
    cname = str(body.character).strip()
    lname = str(body.location).strip()
    cs = int(body.chapter_start)

    try:
        async with character_graph_write_guard(db, project_id) as project_id, neo4j.session() as session:
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
    except CharacterProjectError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None
    except Exception:
        raise HTTPException(status_code=503, detail='人物位置写入未获确认，请保留输入并核对后重试。') from None

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
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set character membership in Neo4j via MEMBER_OF.

    NOTE: We still call materialize as best-effort; Postgres is a projection.
    """
    cname = str(body.character).strip()
    oname = str(body.organization).strip()
    cs = int(body.chapter_start)

    try:
        async with character_graph_write_guard(db, project_id) as project_id, neo4j.session() as session:
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
    except CharacterProjectError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'message': str(exc)}) from None
    except Exception:
        raise HTTPException(status_code=503, detail='人物所属组织写入未获确认，请保留输入并核对后重试。') from None

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
