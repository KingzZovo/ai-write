"""Outline management endpoints."""

import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.neo4j import init_neo4j
from app.db import neo4j as _neo4j_mod
from app.db.session import get_db
from app.models.project import Character, Outline, Relationship, WorldRule
from app.tasks.entity_tasks import _materialize_entities_to_postgres
from app.services.rel_type import canonicalize_rel_type
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

    # v1.9+ architecture: settings are written to Neo4j (source of truth) and
    # materialized back to Postgres as a read model.
    await init_neo4j()
    driver = _neo4j_mod._driver
    if driver is None:
        raise HTTPException(status_code=500, detail="neo4j_not_initialized")

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
        rid = str(uuid.uuid4())
        try:
            async with driver.session() as session:
                neo_res = await session.run(
                    "MERGE (w:WorldRule {project_id: $pid, category: $cat, text: $txt}) "
                    "ON CREATE SET w.id = $id "
                    "RETURN w.id AS id",
                    id=rid,
                    pid=str(project_id),
                    cat=category,
                    txt=rule_text,
                )
                await neo_res.consume()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")
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
        rel_type = canonicalize_rel_type((r.get("rel_type") or "other"))
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
        # Write to Neo4j truth (idempotent MERGE). Materialize will project to PG.
        try:
            async with driver.session() as session:
                # Ensure both nodes exist
                r1 = await session.run(
                    "MERGE (a:Character {project_id: $pid, name: $src}) "
                    "ON CREATE SET a.id = $aid",
                    pid=str(project_id),
                    src=src_name,
                    aid=str(uuid.uuid4()),
                )
                await r1.consume()
                r2 = await session.run(
                    "MERGE (b:Character {project_id: $pid, name: $tgt}) "
                    "ON CREATE SET b.id = $bid",
                    pid=str(project_id),
                    tgt=tgt_name,
                    bid=str(uuid.uuid4()),
                )
                await r2.consume()

                raw_type = str(r.get("rel_type") or "other").strip()

                r3 = await session.run(
                    "MATCH (a:Character {project_id: $pid, name: $src}), "
                    "      (b:Character {project_id: $pid, name: $tgt}) "
                    "MERGE (a)-[rel:RELATES_TO {project_id: $pid, source_name: $src, target_name: $tgt, type: $rtype, chapter_start: $cs}]->(b) "
                    "ON CREATE SET rel.chapter_end = null, rel.raw_type = $raw_type "
                    "SET rel.raw_type = coalesce(rel.raw_type, $raw_type)",
                    pid=str(project_id),
                    src=src_name,
                    tgt=tgt_name,
                    rtype=rel_type,
                    raw_type=raw_type,
                    cs=0,
                )
                await r3.consume()
                rels_created += 1
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"neo4j_write_failed: {e}")

    # Best-effort projection
    await _materialize_entities_to_postgres(
        project_id=str(project_id),
        chapter_idx=0,
        caller="api.outlines.extract_settings",
    )

    return ExtractResponse(
        characters_created=chars_created,
        world_rules_created=rules_created,
        relationships_created=rels_created,
    )
