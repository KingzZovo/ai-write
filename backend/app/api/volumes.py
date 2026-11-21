"""Volume management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Volume, Chapter
from app.api.chapter_order import structure_write
from app.services.chapter_order import reindex_chapter_structure, require_free_index

router = APIRouter(prefix="/api/projects/{project_id}/volumes", tags=["volumes"])


class VolumeCreate(BaseModel):
    title: str
    volume_idx: int = Field(..., strict=True, ge=1, le=2147483647)
    summary: str | None = None


class VolumeUpdate(BaseModel):
    title: str | None = None
    volume_idx: int | None = Field(None, strict=True, ge=1, le=2147483647)
    summary: str | None = None


class VolumeResponse(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    volume_idx: int
    summary: str | None

    model_config = {"from_attributes": True}


class VolumeMove(BaseModel):
    model_config = ConfigDict(extra='forbid')
    neighbor_id: UUID
    expected_idx: int = Field(..., strict=True, ge=1, le=2147483647)
    expected_neighbor_idx: int = Field(..., strict=True, ge=1, le=2147483647)


@router.post('/{volume_id}/move')
async def move_volume(project_id: UUID, volume_id: UUID, body: VolumeMove, db: AsyncSession = Depends(get_db)):
    from app.services.volume_order import swap_adjacent_volumes
    async with structure_write(db, project_id):
        return await swap_adjacent_volumes(db, project_id, volume_id, body.neighbor_id,
                                           body.expected_idx, body.expected_neighbor_idx)


@router.get("")
async def list_volumes(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[VolumeResponse]:
    """List all volumes for a project, ordered by volume_idx."""
    result = await db.execute(
        select(Volume)
        .where(Volume.project_id == UUID(str(project_id)))
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
    async with structure_write(db, project_id):
        await require_free_index(db, Volume, project_id, body.volume_idx)
        volume = Volume(project_id=UUID(str(project_id)), title=body.title,
            volume_idx=body.volume_idx, summary=body.summary)
        db.add(volume)
        await db.flush()
        await reindex_chapter_structure(db, project_id)
        await db.refresh(volume)
        return VolumeResponse.model_validate(volume)


@router.get("/{volume_id}")
async def get_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> VolumeResponse:
    """Get a single volume."""
    volume = await db.get(Volume, UUID(str(volume_id)))
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
    if body.volume_idx is not None:
        async with structure_write(db, project_id):
            return await _update_volume(project_id, volume_id, body, db)
    return await _update_volume(project_id, volume_id, body, db)


async def _update_volume(project_id, volume_id, body, db):
    volume = await db.get(Volume, UUID(str(volume_id)))
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")

    reorder = body.volume_idx is not None and body.volume_idx != volume.volume_idx
    previous_volume_indices = {str(volume.id): volume.volume_idx}
    if reorder:
        await require_free_index(db, Volume, project_id, body.volume_idx, excluding=volume.id)
    if body.title is not None:
        volume.title = body.title
    if body.volume_idx is not None:
        volume.volume_idx = body.volume_idx
    if body.summary is not None:
        volume.summary = body.summary

    await db.flush()
    if reorder:
        await reindex_chapter_structure(db, project_id, previous_volume_indices=previous_volume_indices)
    await db.refresh(volume)
    return VolumeResponse.model_validate(volume)


@router.delete("/{volume_id}", status_code=204)
async def delete_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a volume and all its chapters."""
    async with structure_write(db, project_id):
        volume = await db.get(Volume, UUID(str(volume_id)))
        if not volume or str(volume.project_id) != project_id:
            raise HTTPException(status_code=404, detail="Volume not found")
        await db.delete(volume)
        await db.flush()
        await reindex_chapter_structure(db, project_id)


# =========================================================================
# Volume regenerate (SSE)
# =========================================================================

import json  # noqa: E402
from collections.abc import AsyncGenerator  # noqa: E402

from fastapi.responses import StreamingResponse  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models.project import Chapter, Outline  # noqa: E402
from app.services.outline_generator import OutlineGenerator  # noqa: E402

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/{volume_id}/regenerate")
async def regenerate_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Generate first, then atomically replace the exact version the user saw."""
    from contextlib import aclosing
    from app.db import session
    from app.services.production.writer import guarded_stream
    from app.services.volume_regeneration import snapshot_volume, replace_volume, validate_replacement, VolumeChanged
    try:
        snapshot = await snapshot_volume(db, project_id, volume_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.rollback()  # Release the read transaction before any model work.

    async def generate():
        async with async_session_factory() as check_db:
            current = await snapshot_volume(check_db, project_id, volume_id)
            if current.digest != snapshot.digest:
                raise VolumeChanged('分卷资料已变化，请刷新后重试')
        yield f"data: {json.dumps({'status': 'generating', 'volume_idx': snapshot.volume_idx})}\n\n"
        generator = OutlineGenerator(project_id=project_id)
        stream = await generator.generate_volume_outline(
            book_outline=snapshot.book_data, volume_idx=snapshot.volume_idx, user_notes="", stream=True,
        )
        collected = []
        async with aclosing(stream):
            async for chunk in stream:
                collected.append(chunk)
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        raw = "".join(collected).strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1])
        parsed = json.loads(raw)  # Never repair truncation into destructive replacement.
        validate_replacement(parsed, snapshot)
        async with async_session_factory() as save_db:
            result = await replace_volume(save_db, snapshot, parsed)
            await save_db.commit()
        try:
            from app.services.compass_service import update_on_new_volume
            async with async_session_factory() as compass_db:
                await update_on_new_volume(compass_db, project_id, parsed)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Compass update after volume regeneration failed", exc_info=True)
        yield f"data: {json.dumps({'status': 'done', **result})}\n\n"
        yield "data: [DONE]\n\n"

    async def event_stream():
        try:
            async with aclosing(guarded_stream(session.engine, async_session_factory, project_id,
                                              generate, metadata_only=True)) as stream:
                async for event in stream:
                    yield event
        except Exception:
            # Provider diagnostics must not be returned as user-visible prose.
            import logging
            logging.getLogger(__name__).warning("Volume regeneration failed", exc_info=True)
            yield "data: " + json.dumps({'status': 'failed', 'error': '分卷生成或保存未获确认，请刷新检查当前内容后再操作。'}) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
