"""Outline management endpoints."""

from datetime import datetime
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import init_neo4j
from app.db import neo4j as _neo4j_mod
from app.db.session import get_db
from app.api.chapter_order import structure_write
from app.models.project import Character, Outline, Project
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from app.services.settings_extractor import extract_settings_from_outline, SettingsExtractionError

router = APIRouter(prefix="/api/projects/{project_id}/outlines", tags=["outlines"])


class OutlineCreate(BaseModel):
    level: str  # book, volume, chapter
    parent_id: UUID | None = None
    content_json: dict = {}


class OutlineExpectation(BaseModel):
    expected_content_json: dict | None = None
    expected_version: int | None = None
    expected_is_confirmed: int | None = None


class OutlineUpdate(OutlineExpectation):
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
    created_at: datetime | None = None
    synced_chapters: int = 0
    preserved_chapters: int = 0
    unmaterialized_chapters: int = 0
    save_status: str | None = None

    model_config = {"from_attributes": True}


@asynccontextmanager
async def outline_mutation(db, project_id, outline_id):
    # Determine the immutable level without locking a child before its parent.
    level = await db.scalar(select(Outline.level).where(Outline.id == UUID(str(outline_id)),
        Outline.project_id == UUID(str(project_id))).execution_options(autoflush=False))
    if level == 'volume':
        async with structure_write(db, project_id):
            yield True
    else:
        yield False


def require_outline_expectation(outline, body):
    expected = body.expected_content_json if body is not None else None
    if outline.level == 'volume' and expected is None:
        raise HTTPException(428, detail='请先读取完整卷纲，再保存、确认或删除。')
    if (expected is not None and (outline.content_json or {}) != expected
            or body is not None and body.expected_version is not None and body.expected_version != outline.version
            or body is not None and body.expected_is_confirmed is not None and body.expected_is_confirmed != outline.is_confirmed):
        raise HTTPException(409, detail='大纲已被修改，请重新加载后核对。')


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
    project_id: UUID,
    body: OutlineCreate,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Create a new outline."""
    if body.level == 'volume':
        async with structure_write(db, project_id):
            response = await _create_outline(project_id, body, db)
        response.save_status = 'saved'
        return response
    return await _create_outline(project_id, body, db)


async def _create_outline(project_id, body, db):
    project_id = UUID(str(project_id))
    content = body.content_json
    if body.level == 'volume':
        project = await db.scalar(select(Project).where(Project.id == project_id)
            .with_for_update().execution_options(populate_existing=True))
        if project is None or project.deleted_at is not None:
            raise HTTPException(status_code=404, detail='Project not found')
        from copy import deepcopy
        from app.services.volume_outline_sync import sync_untouched_stubs
        content = deepcopy(content)
        try:
            await sync_untouched_stubs(db, project.id, {}, content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    outline = Outline(
        project_id=project_id,
        level=body.level,
        parent_id=body.parent_id,
        content_json=content,
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
    async with outline_mutation(db, project_id, outline_id) as volume_write:
        response = await _update_outline(project_id, outline_id, body, db)
    if volume_write:
        response.save_status = 'saved'
    return response


async def _update_outline(project_id, outline_id, body, db):
    # Match the generation lock order before touching outlines or chapters.
    project = await db.scalar(select(Project).where(Project.id == UUID(str(project_id)))
                              .with_for_update().execution_options(populate_existing=True))
    if project is None or project.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    outline = await db.scalar(
        select(Outline).where(Outline.id == UUID(str(outline_id)), Outline.project_id == UUID(str(project_id)))
        .with_for_update().execution_options(populate_existing=True)
    )
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")

    require_outline_expectation(outline, body)

    from app.services.volume_outline_sync import SyncResult, sync_untouched_stubs
    sync = SyncResult()
    if body.content_json is not None:
        from app.services.outline_authority import record_text_edit
        body.content_json = record_text_edit(outline.content_json, body.content_json)
        if outline.level == "volume":
            try:
                sync = await sync_untouched_stubs(db, project.id, outline.content_json or {}, body.content_json)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        outline.content_json = body.content_json
        outline.version += 1
    if body.is_confirmed is not None:
        outline.is_confirmed = 1 if body.is_confirmed else 0

    # A failed flush propagates and rolls back the outline and all stub updates.
    await db.flush()
    await db.refresh(outline)
    response = OutlineResponse.model_validate(outline)
    response.synced_chapters = sync.synced
    response.preserved_chapters = sync.preserved
    response.unmaterialized_chapters = sync.unmaterialized
    return response


@router.post("/{outline_id}/confirm")
async def confirm_outline(
    project_id: str,
    outline_id: str,
    body: OutlineExpectation | None = None,
    db: AsyncSession = Depends(get_db),
) -> OutlineResponse:
    """Confirm an outline, marking it as final."""
    async with outline_mutation(db, project_id, outline_id) as volume_write:
        response = await _confirm_outline(project_id, outline_id, body, db)
    if volume_write:
        response.save_status = 'saved'
    return response


async def _confirm_outline(project_id, outline_id, body, db):
    outline = await db.scalar(
        select(Outline).where(Outline.id == UUID(str(outline_id)), Outline.project_id == UUID(str(project_id)))
        .with_for_update().execution_options(populate_existing=True)
    )
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")

    require_outline_expectation(outline, body)

    outline.is_confirmed = 1
    await db.flush()
    await db.refresh(outline)
    return OutlineResponse.model_validate(outline)


class VolumePlanUpdate(BaseModel):
    volume_plan: list[dict]


@router.patch("/{outline_id}/volume-plan")
async def update_volume_plan(
    project_id: str,
    outline_id: str,
    body: VolumePlanUpdate,
    db: AsyncSession = Depends(get_db),
):
    """PR-OL3: update volume_plan in content_json + sync Volume.title.

    Front-end edits the AI-suggested volume plan card (title + est_chapters);
    this endpoint persists the edits and propagates title changes to existing
    Volume rows so step 3 reads the user-curated names.
    """
    from app.models.project import Volume
    from sqlalchemy import select as _select

    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")

    cleaned: list[dict] = []
    for i, item in enumerate(body.volume_plan, start=1):
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "idx": int(item.get("idx") or i),
            "title": str(item.get("title") or f"第{i}卷")[:500],
            "theme": str(item.get("theme") or ""),
            "core_conflict": str(item.get("core_conflict") or ""),
            "est_chapters": int(item.get("est_chapters") or 10),
        })

    cj = dict(outline.content_json or {})
    cj["volume_plan"] = cleaned
    outline.content_json = cj
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(outline, "content_json")

    # Sync Volume.title for matching idx (don't create new ones here; PR-OL2
    # already creates them at outline_book completion).
    vol_rows = (await db.execute(
        _select(Volume).where(Volume.project_id == project_id)
    )).scalars().all()
    by_idx = {v.volume_idx: v for v in vol_rows}
    for item in cleaned:
        v = by_idx.get(item["idx"])
        if v is not None:
            if item["title"] and v.title != item["title"]:
                v.title = item["title"]
            if item["theme"] and not v.summary:
                v.summary = item["theme"]

    await db.commit()
    return {"volume_plan": cleaned, "synced_volumes": len([1 for it in cleaned if it["idx"] in by_idx])}


@router.delete("/{outline_id}", status_code=204)
async def delete_outline(
    project_id: str,
    outline_id: str,
    body: OutlineExpectation | None = None,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an outline."""
    async with outline_mutation(db, project_id, outline_id):
        outline = await db.scalar(select(Outline).where(Outline.id == UUID(str(outline_id)),
            Outline.project_id == UUID(str(project_id))).with_for_update()
            .execution_options(populate_existing=True, autoflush=False))
        if outline is None:
            raise HTTPException(status_code=404, detail='Outline not found')
        require_outline_expectation(outline, body)
        await db.delete(outline)


class ExtractResponse(BaseModel):
    characters_created: int
    world_rules_created: int
    relationships_created: int


@router.post("/{outline_id}/extract-settings", response_model=ExtractResponse)
async def extract_settings(
    project_id: str,
    outline_id: str,
    db: AsyncSession = Depends(get_db),
) -> ExtractResponse:
    """Extract structured Character + WorldRule records from a book outline via LLM.

    Safe to call multiple times: skips characters that already exist with the
    same (project_id, name) and rules with the same (project_id, category, rule_text).
    """
    from app.services.outline_settings_source import (
        SettingsSourceError, read_settings_source,
    )
    try:
        source = await read_settings_source(db, project_id, outline_id)
    except SettingsSourceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    from app.services.outline_settings_delivery import find_receipt, save_receipt, deliver_receipt, apply_payload
    receipt = await find_receipt(db, source)
    if receipt is None:
        await db.rollback()
        try:
            extracted = await extract_settings_from_outline(source.text, project_id=source.project_id)
        except SettingsExtractionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        try:
            receipt = await save_receipt(db, source, extracted)
        except SettingsSourceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def apply(db, source, payload):
        await init_neo4j()
        return await apply_payload(db, source, payload, driver=_neo4j_mod._driver,
                                   materialize=_materialize_entities_to_postgres)
    receipt = await deliver_receipt(db, receipt, apply=apply)
    if receipt is None or receipt.status == 'stale':
        raise HTTPException(status_code=409, detail="书纲已变化，请刷新后重新提取。")
    if receipt.status != 'completed':
        raise HTTPException(status_code=503, detail="设定同步未完成，结果已保留，将自动重试。")
    counts = receipt.counts_json or {}
    return ExtractResponse(characters_created=counts.get('chars_created', 0),
        world_rules_created=counts.get('rules_created', 0), relationships_created=counts.get('rels_created', 0))


@router.get("/{outline_id}/extract-settings")
async def settings_extraction_status(project_id: str, outline_id: str, db: AsyncSession = Depends(get_db)):
    from app.services.outline_settings_source import read_settings_source, SettingsSourceError
    from app.services.outline_settings_delivery import find_receipt
    try:
        source = await read_settings_source(db, project_id, outline_id)
    except SettingsSourceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    receipt = await find_receipt(db, source)
    if receipt is None:
        return {'status': 'not_started'}
    counts = receipt.counts_json or {}
    return {'status': receipt.status, 'attempts': receipt.attempts,
            'counts': {'characters_created': counts.get('chars_created', 0),
                       'world_rules_created': counts.get('rules_created', 0),
                       'relationships_created': counts.get('rels_created', 0)}}
