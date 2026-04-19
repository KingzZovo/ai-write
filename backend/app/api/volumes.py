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
    """Delete existing chapters + volume outline and regenerate via SSE."""
    volume = await db.get(Volume, volume_id)
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")

    # Find confirmed book outline
    book_result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "book",
            Outline.is_confirmed == 1,
        ).order_by(Outline.created_at.asc())
    )
    book_outline = book_result.scalar_one_or_none()
    if not book_outline:
        raise HTTPException(status_code=400, detail="No confirmed book outline found")
    book_outline_data = book_outline.content_json or {}
    book_outline_id = book_outline.id
    volume_idx = volume.volume_idx

    # Delete existing chapters under this volume
    ch_result = await db.execute(select(Chapter).where(Chapter.volume_id == volume_id))
    for ch in ch_result.scalars().all():
        await db.delete(ch)

    # Delete existing volume outlines for this volume_idx
    ol_result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "volume",
            Outline.parent_id == book_outline_id,
        )
    )
    for ol in ol_result.scalars().all():
        cj = ol.content_json or {}
        if isinstance(cj, dict) and cj.get("volume_idx") == volume_idx:
            await db.delete(ol)

    await db.flush()

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            yield f"data: {json.dumps({'status': 'generating', 'volume_idx': volume_idx})}\n\n"
            collected: list[str] = []
            generator = OutlineGenerator()
            async for chunk in await generator.generate_volume_outline(
                book_outline=book_outline_data,
                volume_idx=volume_idx,
                user_notes="",
                stream=True,
            ):
                collected.append(chunk)
                yield f"data: {json.dumps({'text': chunk})}\n\n"

            full = "".join(collected).strip()
            if not full:
                yield f"data: {json.dumps({'error': 'LLM returned empty'})}\n\n"
                return

            cleaned = full
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                parsed = {"raw_text": full}
            if not isinstance(parsed, dict):
                parsed = {"raw_text": full}

            chapters_created = 0
            async with async_session_factory() as save_db:
                new_ol = Outline(
                    project_id=project_id,
                    level="volume",
                    parent_id=book_outline_id,
                    content_json=parsed,
                )
                save_db.add(new_ol)

                vol = await save_db.get(Volume, volume_id)
                if isinstance(parsed.get("title"), str) and parsed["title"].strip():
                    vol.title = parsed["title"].strip()
                summary = parsed.get("core_conflict") or parsed.get("emotional_arc")
                if isinstance(summary, str):
                    vol.summary = summary

                chs = parsed.get("chapter_summaries") if isinstance(parsed, dict) else None
                if isinstance(chs, list):
                    for i, cs in enumerate(chs):
                        if not isinstance(cs, dict):
                            continue
                        chapter_idx = cs.get("chapter_idx") if isinstance(cs.get("chapter_idx"), int) else i + 1
                        title_raw = cs.get("title")
                        title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else f"第{chapter_idx}章"
                        save_db.add(Chapter(
                            volume_id=volume_id,
                            title=title,
                            chapter_idx=chapter_idx,
                            outline_json=cs,
                        ))
                        chapters_created += 1
                await save_db.commit()

            yield f"data: {json.dumps({'status': 'done', 'chapters_created': chapters_created})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
