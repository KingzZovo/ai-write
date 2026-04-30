"""Outline management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Character, Outline, Relationship, WorldRule
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

    # Flush so newly-created characters have IDs available for relationship mapping
    await db.flush()
    name_to_id_result = await db.execute(
        select(Character.id, Character.name).where(Character.project_id == project_id)
    )
    name_to_id: dict[str, str] = {name: str(cid) for cid, name in name_to_id_result.all()}

    rels_created = 0
    for r in extracted.get("relationships", []):
        if not isinstance(r, dict):
            continue
        src_name = (r.get("source_name") or "").strip()
        tgt_name = (r.get("target_name") or "").strip()
        src = name_to_id.get(src_name)
        tgt = name_to_id.get(tgt_name)
        if not src or not tgt or src == tgt:
            continue
        rel_type = (r.get("rel_type") or "other").strip()
        # rel_type is used downstream for behavior/OOC checks. Keep it short and stable.
        # Prefer canonical keywords like: 敌对/对立/盟友/朋友/恋人/师徒/上下级/监管/同伴/同舍/其他
        # If the extractor returned verbose descriptions, normalize to a compact token.
        raw_rel_type = rel_type
        if "（" in rel_type:
            rel_type = rel_type.split("（", 1)[0].strip()
        if "(" in rel_type:
            rel_type = rel_type.split("(", 1)[0].strip()
        # common pattern: "A/B（...）" -> "A"
        if "/" in rel_type:
            rel_type = rel_type.split("/", 1)[0].strip()
        # keyword canonicalization
        if any(k in raw_rel_type for k in ["敌对", "仇敌", "死敌"]):
            rel_type = "敌对"
        elif any(k in raw_rel_type for k in ["对立", "不信任", "对手"]):
            rel_type = "对立"
        elif any(k in raw_rel_type for k in ["监管", "押解", "押送", "看押", "管辖", "盘查", "监控", "审查", "取证"]):
            rel_type = "监管"
        elif any(k in raw_rel_type for k in ["审讯", "逼问"]):
            rel_type = "审讯"
        elif any(k in raw_rel_type for k in ["师生", "师徒"]):
            rel_type = "师生"
        elif any(k in raw_rel_type for k in ["上下级", "上位", "下属"]):
            rel_type = "上下级"
        elif any(k in raw_rel_type for k in ["同舍", "同寝"]):
            rel_type = "同舍"
        elif any(k in raw_rel_type for k in ["同伴", "同学", "同行", "协作"]):
            rel_type = "同伴"
        elif any(k in raw_rel_type for k in ["失联", "寻找"]):
            rel_type = "失联"
        # DB field is VARCHAR(50)
        rel_type = (rel_type or "other")[:50]
        label = (r.get("label") or "").strip()
        note = (r.get("note") or "").strip()
        sentiment = (r.get("sentiment") or "neutral").strip()
        # Idempotency: rel_type is canonicalized; treat (src,tgt,rel_type) as the identity.
        # label/note/sentiment are descriptive and may vary across extraction runs.
        dup = await db.execute(
            select(Relationship.id).where(
                Relationship.project_id == project_id,
                Relationship.source_id == src,
                Relationship.target_id == tgt,
                Relationship.rel_type == rel_type,
            )
        )
        if dup.scalar_one_or_none():
            continue
        # Use a SAVEPOINT so unique-constraint conflicts don't abort the whole request.
        try:
            async with db.begin_nested():
                db.add(Relationship(
                    project_id=project_id,
                    source_id=src,
                    target_id=tgt,
                    rel_type=rel_type,
                    label=label,
                    note=note,
                    sentiment=sentiment,
                ))
                await db.flush()
                rels_created += 1
        except IntegrityError:
            # DB has uq_relationships_rel_key; treat as already-created.
            continue

    return ExtractResponse(
        characters_created=chars_created,
        world_rules_created=rules_created,
        relationships_created=rels_created,
    )
