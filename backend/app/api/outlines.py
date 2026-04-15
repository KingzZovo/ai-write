"""Outline management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Outline

router = APIRouter(prefix="/api/projects/{project_id}/outlines", tags=["outlines"])


class OutlineCreate(BaseModel):
    level: str  # book, volume, chapter
    parent_id: str | None = None
    content_json: dict = {}


class OutlineUpdate(BaseModel):
    content_json: dict | None = None
    is_confirmed: bool | None = None


class OutlineResponse(BaseModel):
    id: UUID
    project_id: UUID
    level: str
    parent_id: UUID | None
    content_json: dict
    version: int
    is_confirmed: int

    model_config = {"from_attributes": True}


@router.get("")
async def list_outlines(
    project_id: str,
    level: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[OutlineResponse]:
    """List outlines, optionally filtered by level."""
    query = select(Outline).where(Outline.project_id == project_id)
    if level:
        query = query.where(Outline.level == level)
    result = await db.execute(query)
    outlines = result.scalars().all()
    return [OutlineResponse.model_validate(o) for o in outlines]


@router.post("", status_code=201)
async def create_outline(
    project_id: str,
    body: OutlineCreate,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Create a new outline."""
    outline = Outline(
        project_id=project_id,
        level=body.level,
        parent_id=body.parent_id,
        content_json=body.content_json,
    )
    db.add(outline)
    await db.flush()
    await db.refresh(outline)
    return OutlineResponse.model_validate(outline)


@router.get("/{outline_id}")
async def get_outline(
    project_id: str,
    outline_id: str,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Get a single outline."""
    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")
    return OutlineResponse.model_validate(outline)


@router.put("/{outline_id}")
async def update_outline(
    project_id: str,
    outline_id: str,
    body: OutlineUpdate,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Update an outline (e.g., user fine-tuning)."""
    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")

    if body.content_json is not None:
        outline.content_json = body.content_json
        outline.version += 1
    if body.is_confirmed is not None:
        outline.is_confirmed = 1 if body.is_confirmed else 0

    await db.flush()
    await db.refresh(outline)
    return OutlineResponse.model_validate(outline)


@router.post("/{outline_id}/confirm")
async def confirm_outline(
    project_id: str,
    outline_id: str,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Confirm an outline, marking it as final."""
    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")

    outline.is_confirmed = 1
    await db.flush()
    await db.refresh(outline)
    return OutlineResponse.model_validate(outline)


@router.delete("/{outline_id}", status_code=204)
async def delete_outline(
    project_id: str,
    outline_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an outline."""
    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")
    await db.delete(outline)
