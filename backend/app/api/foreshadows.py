"""Foreshadow management endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Foreshadow

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
    resolve_conditions_json: list[Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float
    status: str
    resolved_chapter: int | None = None
    created_at: Any


class ForeshadowListResponse(BaseModel):
    foreshadows: list[ForeshadowResponse]
    total: int


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

    return ForeshadowListResponse(
        foreshadows=[ForeshadowResponse.model_validate(f) for f in foreshadows],
        total=len(foreshadows),
    )


@router.post("", response_model=ForeshadowResponse, status_code=201)
async def create_foreshadow(
    project_id: UUID,
    body: ForeshadowCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Create a foreshadow manually."""
    foreshadow = Foreshadow(
        project_id=project_id,
        type=body.type,
        description=body.description,
        planted_chapter=body.planted_chapter,
        resolve_conditions_json=body.resolve_conditions,
        resolution_blueprint_json=body.resolution_blueprint or {},
        narrative_proximity=0.0,
        status="planted",
    )
    db.add(foreshadow)
    await db.flush()
    await db.refresh(foreshadow)
    return ForeshadowResponse.model_validate(foreshadow)


@router.put("/{foreshadow_id}", response_model=ForeshadowResponse)
async def update_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Update a foreshadow."""
    result = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = result.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=404, detail="Foreshadow not found")

    update_data = body.model_dump(exclude_unset=True)

    # Map API field names to ORM column names
    field_mapping = {
        "resolve_conditions": "resolve_conditions_json",
        "resolution_blueprint": "resolution_blueprint_json",
    }

    for api_field, value in update_data.items():
        orm_field = field_mapping.get(api_field, api_field)
        setattr(foreshadow, orm_field, value)

    await db.flush()
    await db.refresh(foreshadow)
    return ForeshadowResponse.model_validate(foreshadow)


@router.delete("/{foreshadow_id}", status_code=204)
async def delete_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a foreshadow."""
    result = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = result.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=404, detail="Foreshadow not found")

    await db.delete(foreshadow)
    await db.flush()


@router.post("/{foreshadow_id}/resolve", response_model=ForeshadowResponse)
async def resolve_foreshadow(
    project_id: UUID,
    foreshadow_id: UUID,
    body: ForeshadowResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> ForeshadowResponse:
    """Manually mark a foreshadow as resolved."""
    result = await db.execute(
        select(Foreshadow).where(
            Foreshadow.id == foreshadow_id,
            Foreshadow.project_id == project_id,
        )
    )
    foreshadow = result.scalar_one_or_none()
    if foreshadow is None:
        raise HTTPException(status_code=404, detail="Foreshadow not found")

    if foreshadow.status == "resolved":
        raise HTTPException(status_code=400, detail="Foreshadow is already resolved")

    foreshadow.status = "resolved"
    foreshadow.resolved_chapter = body.resolved_chapter
    foreshadow.narrative_proximity = 1.0

    await db.flush()
    await db.refresh(foreshadow)
    return ForeshadowResponse.model_validate(foreshadow)
