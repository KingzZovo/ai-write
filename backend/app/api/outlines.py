"""Outline management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Character, Outline, WorldRule
from app.services.settings_extractor import extract_settings_from_outline

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


class ExtractResponse(BaseModel):
    characters_created: int
    world_rules_created: int


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
    outline = await db.get(Outline, outline_id)
    if not outline or str(outline.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Outline not found")
    if outline.level != "book":
        raise HTTPException(status_code=400, detail="Extraction only supports book-level outlines")

    cj = outline.content_json or {}
    raw_text = cj.get("raw_text") if isinstance(cj, dict) else None
    if not raw_text:
        raise HTTPException(status_code=400, detail="Outline has no raw_text to extract from")

    extracted = await extract_settings_from_outline(raw_text)

    # Dedupe against existing rows
    existing_char_names = set()
    result = await db.execute(
        select(Character.name).where(Character.project_id == project_id)
    )
    for name in result.scalars().all():
        existing_char_names.add(name)

    existing_rule_keys = set()
    result = await db.execute(
        select(WorldRule.category, WorldRule.rule_text).where(
            WorldRule.project_id == project_id
        )
    )
    for cat, txt in result.all():
        existing_rule_keys.add((cat, txt))

    chars_created = 0
    for c in extracted.get("characters", []):
        name = (c.get("name") or "").strip() if isinstance(c, dict) else ""
        if not name or name in existing_char_names:
            continue
        db.add(Character(project_id=project_id, name=name, profile_json=c))
        existing_char_names.add(name)
        chars_created += 1

    rules_created = 0
    for r in extracted.get("world_rules", []):
        if not isinstance(r, dict):
            continue
        category = (r.get("category") or "").strip()
        rule_text = (r.get("rule_text") or "").strip()
        if not category or not rule_text:
            continue
        key = (category, rule_text)
        if key in existing_rule_keys:
            continue
        db.add(WorldRule(project_id=project_id, category=category, rule_text=rule_text))
        existing_rule_keys.add(key)
        rules_created += 1

    await db.flush()
    return ExtractResponse(
        characters_created=chars_created,
        world_rules_created=rules_created,
    )
