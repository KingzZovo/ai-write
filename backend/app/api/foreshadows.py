"""Foreshadow management endpoints."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from neo4j import AsyncDriver
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import get_neo4j
from app.db.session import get_db
from app.models.project import Foreshadow
from app.tasks.entity_tasks import _materialize_entities_to_postgres

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/foreshadows",
    tags=["foreshadows"],
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ForeshadowCreateRequest(BaseModel):
    type: str = Field(..., max_length=20, description="e.g. plot, character, worldbuilding, mystery")
    description: str = Field(..., description="What the foreshadow is about")
    planted_chapter: int = Field(..., ge=0, description="Chapter index where the foreshadow was planted")
    resolve_conditions: list[str] = Field(
        default_factory=list,
        description="Narrative conditions that would make resolution natural",
    )
    resolution_blueprint: dict[str, Any] | None = Field(
        None, description="Optional guidance on how to resolve"
    )


class ForeshadowUpdateRequest(BaseModel):
    type: str | None = Field(None, max_length=20)
    description: str | None = None
    planted_chapter: int | None = Field(None, ge=0)
    resolve_conditions: list[str] | None = None
    resolution_blueprint: dict[str, Any] | None = None
    narrative_proximity: float | None = Field(None, ge=0.0, le=1.0)
    status: str | None = Field(None, max_length=20)
    resolved_chapter: int | None = None


class ForeshadowResolveRequest(BaseModel):
    resolved_chapter: int = Field(..., ge=0, description="Chapter where foreshadow was resolved")


class ForeshadowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    type: str
    description: str
    planted_chapter: int
    resolve_conditions_json: list[Any] | dict[str, Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float | None = None
    status: str
    resolved_chapter: int | None = None
    created_at: Any


class ForeshadowListResponse(BaseModel):
    foreshadows: list[ForeshadowResponse]
    total: int
    # Q4: foreshadow debt health (QMAI-adapted):
    # {"score": int, "criticals": [...], "warnings": [...], "unresolved": int}
    debt: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ForeshadowListResponse)
async def list_foreshadows(
    project_id: UUID,
    status: str | None = Query(None, description="Filter by status: planted, ripening, ready, resolved"),
    db: AsyncSession = Depends(get_db),
) -> ForeshadowListResponse:
    """List all foreshadows for a project, optionally filtered by status."""
    query = select(Foreshadow).where(
        Foreshadow.project_id == project_id
    )
    if status is not None:
        query = query.where(Foreshadow.status == status)
    query = query.order_by(Foreshadow.planted_chapter)

    result = await db.execute(query)
    foreshadows = list(result.scalars().all())

    # Q4: attach foreshadow debt health. Debt is always computed over the
    # full (unfiltered) foreshadow set so a status filter cannot skew the
    # score. Fail-safe: a debt computation problem never breaks listing.
    debt: dict[str, Any] | None = None
    try:
        from app.services.foreshadow_manager import compute_debt_score

        if status is None:
            debt_rows = foreshadows
        else:
            all_result = await db.execute(
                select(Foreshadow).where(Foreshadow.project_id == project_id)
            )
            debt_rows = list(all_result.scalars().all())
        debt = compute_debt_score(
            debt_rows,
            await _current_global_chapter_idx(db, project_id, debt_rows),
        )
    except Exception:
        logger.warning(
            "foreshadow debt computation failed for project %s",
            project_id,
            exc_info=True,
        )

    return ForeshadowListResponse(
        foreshadows=[ForeshadowResponse.model_validate(f) for f in foreshadows],
        total=len(foreshadows),
        debt=debt,
    )


async def _current_global_chapter_idx(
    db: AsyncSession,
    project_id: UUID,
    foreshadows: list[Foreshadow],
) -> int:
    """Best-effort book-global writing progress for debt aging.

    Primary source: the number of chapters with generated content across the
    project (chapter rows are per-volume, so the count is the global
    progress). Fallback when nothing is written yet or the query fails: the
    max planted/resolved chapter recorded on the foreshadows themselves.
    """
    written = 0
    try:
        from sqlalchemy import func

        from app.models.project import Chapter, Volume

        count_result = await db.execute(
            select(func.count(Chapter.id))
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(
                Volume.project_id == project_id,
                Chapter.content_text.isnot(None),
                Chapter.content_text != "",
            )
        )
        written = int(count_result.scalar() or 0)
    except Exception:
        logger.debug("written-chapter count failed for project %s", project_id, exc_info=True)
    if written > 0:
        return written
    fallback = 0
    for fs in foreshadows:
        for value in (fs.planted_chapter, fs.resolved_chapter):
            if value is not None:
                fallback = max(fallback, int(value))
    return fallback


@router.post("", response_model=ForeshadowResponse, status_code=201)
async def create_foreshadow(
    project_id: UUID,
    body: ForeshadowCreateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Create a foreshadow.

    v1.9+ recommended architecture: write to Neo4j (source of truth) and
    materialize into Postgres (read model).
    """
    fid = str(uuid.uuid4())
    try:
        async with neo4j.session() as session:
            r = await session.run(
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
                type=str(body.type).strip(),
                desc=str(body.description).strip(),
                planted=int(body.planted_chapter),
                conds=json.dumps(list(body.resolve_conditions or []), ensure_ascii=False),
                blueprint=json.dumps(body.resolution_blueprint or {}, ensure_ascii=False),
                prox=0.0,
                status="planted",
                resolved=None,
            )
            await r.consume()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=int(body.planted_chapter),
        caller="api.foreshadows.create",
    )

    row = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == UUID(fid),
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = row.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=500, detail="pg_materialize_missing")
    return ForeshadowResponse.model_validate(foreshadow)


@router.put("/{foreshadow_id}", response_model=ForeshadowResponse)
async def update_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowUpdateRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Update a foreshadow.

    Writes to Neo4j (source of truth) and materializes to Postgres.
    """
    update_data = body.model_dump(exclude_unset=True)

    # Map API field names to ORM column names
    field_mapping = {
        "resolve_conditions": "resolve_conditions_json",
        "resolution_blueprint": "resolution_blueprint_json",
    }

    # Build patch for Neo4j.
    allowed = {
        "type",
        "description",
        "planted_chapter",
        "resolve_conditions_json",
        "resolution_blueprint_json",
        "narrative_proximity",
        "status",
        "resolved_chapter",
    }
    neo4j_updates: dict[str, Any] = {}
    for api_field, value in update_data.items():
        orm_field = field_mapping.get(api_field, api_field)
        if orm_field in allowed:
            neo4j_updates[orm_field] = value

    try:
        async with neo4j.session() as session:
            # Ensure the node exists.
            r0 = await session.run(
                "MATCH (f:Foreshadow {project_id: $pid, id: $id}) RETURN f.id AS id",
                pid=str(project_id),
                id=str(foreshadow_id),
            )
            rec0 = await r0.single()
            if rec0 is None:
                raise HTTPException(status_code=404, detail="Foreshadow not found")

            # Apply updates (SET only provided fields).
            set_parts = []
            params: dict[str, Any] = {"pid": str(project_id), "id": str(foreshadow_id)}
            for k, v in neo4j_updates.items():
                if k in ("resolve_conditions_json", "resolution_blueprint_json"):
                    params[k] = json.dumps(v or ([] if k == "resolve_conditions_json" else {}), ensure_ascii=False)
                else:
                    params[k] = v
                set_parts.append(f"f.{k} = ${k}")

            if set_parts:
                q = (
                    "MATCH (f:Foreshadow {project_id: $pid, id: $id}) "
                    "SET "
                    + ", ".join(set_parts)
                )
                r1 = await session.run(q, **params)
                await r1.consume()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    # Materialize and return PG read model.
    chapter_idx = int(neo4j_updates.get("planted_chapter") or 0)
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=chapter_idx,
        caller="api.foreshadows.update",
    )
    row = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = row.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=500, detail="pg_materialize_missing")
    return ForeshadowResponse.model_validate(foreshadow)


@router.delete("/{foreshadow_id}", status_code=204)
async def delete_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a foreshadow.

    Deletes from Neo4j (source of truth), then materializes to Postgres.
    """
    try:
        async with neo4j.session() as session:
            r = await session.run(
                "MATCH (f:Foreshadow {project_id: $pid, id: $id}) DETACH DELETE f",
                pid=str(project_id),
                id=str(foreshadow_id),
            )
            summary = await r.consume()
            # If the node didn't exist, treat as 404 to match previous behavior.
            if summary.counters.nodes_deleted == 0:
                raise HTTPException(status_code=404, detail="Foreshadow not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.foreshadows.delete",
    )

    # Best-effort cleanup in PG if row exists (materialize should handle it too,
    # but current materialize implementation is upsert-only).
    result = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = result.scalar_one_or_none()
    if foreshadow is not None:
        await db.delete(foreshadow)
        await db.flush()


@router.post("/{foreshadow_id}/resolve", response_model=ForeshadowResponse)
async def resolve_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowResolveRequest,
    neo4j: AsyncDriver = Depends(get_neo4j),
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Manually mark a foreshadow as resolved.

    Writes to Neo4j (source of truth), materializes to Postgres.
    """
    try:
        async with neo4j.session() as session:
            # Ensure exists and not already resolved.
            r0 = await session.run(
                "MATCH (f:Foreshadow {project_id: $pid, id: $id}) "
                "RETURN f.status AS status",
                pid=str(project_id),
                id=str(foreshadow_id),
            )
            rec0 = await r0.single()
            if rec0 is None:
                raise HTTPException(status_code=404, detail="Foreshadow not found")
            if rec0.get("status") == "resolved":
                raise HTTPException(status_code=400, detail="Foreshadow is already resolved")

            r1 = await session.run(
                "MATCH (f:Foreshadow {project_id: $pid, id: $id}) "
                "SET f.status = 'resolved', "
                "    f.resolved_chapter = $resolved_chapter, "
                "    f.narrative_proximity = 1.0",
                pid=str(project_id),
                id=str(foreshadow_id),
                resolved_chapter=int(body.resolved_chapter),
            )
            await r1.consume()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=int(body.resolved_chapter),
        caller="api.foreshadows.resolve",
    )

    row = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = row.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=500, detail="pg_materialize_missing")
    return ForeshadowResponse.model_validate(foreshadow)
