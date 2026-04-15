"""Volume management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.project import Volume, Chapter

router = APIRouter(prefix="/api/projects/{project_id}/volumes", tags=["volumes"])


class VolumeCreate(BaseModel):
    title: str
    volume_idx: int
    summary: str | None = None


class VolumeUpdate(BaseModel):
    title: str | None = None
    volume_idx: int | None = None
    summary: str | None = None


class VolumeResponse(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    volume_idx: int
    summary: str | None

    model_config = {"from_attributes": True}


@router.get("")
async def list_volumes(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[VolumeResponse]:
    """List all volumes for a project, ordered by volume_idx."""
    result = await db.execute(
        select(Volume)
        .where(Volume.project_id == project_id)
        .order_by(Volume.volume_idx)
    )
    return [VolumeResponse.model_validate(v) for v in result.scalars().all()]


@router.post("", status_code=201)
async def create_volume(
    project_id: str,
    body: VolumeCreate,
    db: AsyncSession = Depends(get_db),
) -> VolumeResponse:
    """Create a new volume."""
    volume = Volume(
        project_id=project_id,
        title=body.title,
        volume_idx=body.volume_idx,
        summary=body.summary,
    )
    db.add(volume)
    await db.flush()
    await db.refresh(volume)
    return VolumeResponse.model_validate(volume)


@router.get("/{volume_id}")
async def get_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> VolumeResponse:
    """Get a single volume."""
    volume = await db.get(Volume, volume_id)
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    return VolumeResponse.model_validate(volume)


@router.put("/{volume_id}")
async def update_volume(
    project_id: str,
    volume_id: str,
    body: VolumeUpdate,
    db: AsyncSession = Depends(get_db),
) -> VolumeResponse:
    """Update a volume."""
    volume = await db.get(Volume, volume_id)
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")

    if body.title is not None:
        volume.title = body.title
    if body.volume_idx is not None:
        volume.volume_idx = body.volume_idx
    if body.summary is not None:
        volume.summary = body.summary

    await db.flush()
    await db.refresh(volume)
    return VolumeResponse.model_validate(volume)


@router.delete("/{volume_id}", status_code=204)
async def delete_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a volume and all its chapters."""
    volume = await db.get(Volume, volume_id)
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    await db.delete(volume)
