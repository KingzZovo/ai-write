"""Story settings (设定集) management endpoints.

Covers characters and world rules for a project.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Character, WorldRule
from app.services.change_log import record_change


router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["settings"],
)


# NOTE (v1.9+): Neo4j is the source of truth for settings entities like
# world_rules / relationships. Postgres tables are read-optimized projections
# materialized from Neo4j. To avoid drift, legacy Postgres write endpoints are
# disabled; use /neo4j-settings/* instead.
LEGACY_SETTINGS_WRITE_DISABLED_DETAIL = (
    "Legacy Postgres settings write endpoints are disabled (v1.9+). "
    "Write to Neo4j via /neo4j-settings/* and materialize back to Postgres."
)


# =========================================================================
# Character schemas
# =========================================================================


class CharacterCreateRequest(BaseModel):
    name: str = Field(..., max_length=200)
    profile_json: dict[str, Any] | None = Field(
        None,
        description="Structured character profile (age, personality, abilities, etc.)",
    )


class CharacterUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=200)
    profile_json: dict[str, Any] | None = None


class CharacterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    profile_json: dict[str, Any] | None = None
    created_at: Any


class CharacterListResponse(BaseModel):
    characters: list[CharacterResponse]
    total: int


# =========================================================================
# WorldRule schemas
# =========================================================================


class WorldRuleCreateRequest(BaseModel):
    category: str = Field(..., max_length=100, description="e.g. magic_system, geography, politics")
    rule_text: str = Field(..., description="The rule description text")


class WorldRuleUpdateRequest(BaseModel):
    category: str | None = Field(None, max_length=100)
    rule_text: str | None = None


class WorldRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    category: str
    rule_text: str
    created_at: Any


class WorldRuleListResponse(BaseModel):
    world_rules: list[WorldRuleResponse]
    total: int


# =========================================================================
# Character endpoints
# =========================================================================


@router.get("/characters", response_model=CharacterListResponse)
async def list_characters(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> CharacterListResponse:
    """List all characters for a project."""
    result = await db.execute(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at)
    )
    characters = list(result.scalars().all())
    return CharacterListResponse(
        characters=[CharacterResponse.model_validate(c) for c in characters],
        total=len(characters),
    )


@router.post("/characters", response_model=CharacterResponse, status_code=201)
async def create_character(
    project_id: UUID,
    body: CharacterCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> CharacterResponse:
    """Create a new character."""
    character = Character(
        project_id=project_id,
        name=body.name,
        profile_json=body.profile_json or {},
    )
    db.add(character)
    await db.flush()
    await db.refresh(character)
    await record_change(
        db,
        project_id=project_id,
        target_type="character",
        target_id=character.id,
        action="create",
        after={"name": character.name, "profile_json": character.profile_json},
    )
    return CharacterResponse.model_validate(character)


@router.put("/characters/{character_id}", response_model=CharacterResponse)
async def update_character(
    project_id: UUID,
    character_id: UUID,
    body: CharacterUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> CharacterResponse:
    """Update a character."""
    result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.project_id == project_id,
        )
    )
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")

    before_state = {"name": character.name, "profile_json": character.profile_json}
    update_data = body.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(character, field_name, value)

    await db.flush()
    await db.refresh(character)
    await record_change(
        db,
        project_id=project_id,
        target_type="character",
        target_id=character.id,
        action="update",
        before=before_state,
        after={"name": character.name, "profile_json": character.profile_json},
    )
    return CharacterResponse.model_validate(character)


@router.delete("/characters/{character_id}", status_code=204)
async def delete_character(
    project_id: UUID,
    character_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a character."""
    result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.project_id == project_id,
        )
    )
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")

    before_state = {"name": character.name, "profile_json": character.profile_json}
    deleted_id = character.id
    await db.delete(character)
    await db.flush()
    await record_change(
        db,
        project_id=project_id,
        target_type="character",
        target_id=deleted_id,
        action="delete",
        before=before_state,
    )


# =========================================================================
# WorldRule endpoints
# =========================================================================


@router.get("/world-rules", response_model=WorldRuleListResponse)
async def list_world_rules(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> WorldRuleListResponse:
    """List all world rules for a project."""
    result = await db.execute(
        select(WorldRule)
        .where(WorldRule.project_id == project_id)
        .order_by(WorldRule.created_at)
    )
    rules = list(result.scalars().all())
    return WorldRuleListResponse(
        world_rules=[WorldRuleResponse.model_validate(r) for r in rules],
        total=len(rules),
    )


@router.post("/world-rules", response_model=WorldRuleResponse, status_code=201)
async def create_world_rule(
    project_id: UUID,
    body: WorldRuleCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> WorldRuleResponse:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


@router.put("/world-rules/{rule_id}", response_model=WorldRuleResponse)
async def update_world_rule(
    project_id: UUID,
    rule_id: UUID,
    body: WorldRuleUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> WorldRuleResponse:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


@router.delete("/world-rules/{rule_id}", status_code=204)
async def delete_world_rule(
    project_id: UUID,
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


# =========================================================================
# Relationships
# =========================================================================

from app.schemas.project import (  # noqa: E402
    RelationshipCreate,
    RelationshipUpdate,
    RelationshipResponse,
    RelationshipListResponse,
    RelationshipBulkRequest,
)
from app.models.project import Relationship  # noqa: E402


class RelationshipBulkResponse(BaseModel):
    created: int


@router.get("/relationships", response_model=RelationshipListResponse)
async def list_relationships(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> RelationshipListResponse:
    result = await db.execute(
        select(Relationship).where(Relationship.project_id == project_id)
    )
    items = list(result.scalars().all())
    return RelationshipListResponse(
        relationships=[RelationshipResponse.model_validate(r) for r in items],
        total=len(items),
    )


@router.post("/relationships", response_model=RelationshipResponse, status_code=201)
async def create_relationship(
    project_id: str,
    body: RelationshipCreate,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


@router.post("/relationships/bulk", response_model=RelationshipBulkResponse, status_code=201)
async def bulk_create_relationships(
    project_id: str,
    body: RelationshipBulkRequest,
    db: AsyncSession = Depends(get_db),
) -> RelationshipBulkResponse:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


@router.put("/relationships/{relationship_id}", response_model=RelationshipResponse)
async def update_relationship(
    project_id: str,
    relationship_id: str,
    body: RelationshipUpdate,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


@router.delete("/relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    project_id: str,
    relationship_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    raise HTTPException(status_code=410, detail=LEGACY_SETTINGS_WRITE_DISABLED_DETAIL)


# =========================================================================
# v0.9: Relationship evolution & as-of snapshot
# =========================================================================


class RelationshipEvolutionEntry(BaseModel):
    volume_id: UUID
    label: str | None = None
    sentiment: str | None = None
    note: str | None = None


@router.post(
    "/relationships/{relationship_id}/evolution",
    response_model=RelationshipResponse,
)
async def append_relationship_evolution(
    project_id: str,
    relationship_id: str,
    body: RelationshipEvolutionEntry,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    """Append an evolution entry for a relationship at a given volume.

    Evolution entries describe how a relationship's label/sentiment/note
    shift as the story progresses. The ``evolution_json`` column holds an
    ordered list of ``{volume_id, label?, sentiment?, note?}`` dicts.
    """
    rel = await db.get(Relationship, relationship_id)
    if rel is None or str(rel.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Relationship not found")

    entry = body.model_dump(mode="json", exclude_none=True)
    before_state = {"evolution_json": list(rel.evolution_json or [])}
    new_list = list(rel.evolution_json or [])
    new_list.append(entry)
    rel.evolution_json = new_list
    await db.flush()
    await db.refresh(rel)
    await record_change(
        db,
        project_id=project_id,
        target_type="relationship",
        target_id=rel.id,
        action="update",
        before=before_state,
        after={"evolution_json": list(rel.evolution_json or [])},
        reason="evolution append",
    )
    return RelationshipResponse.model_validate(rel)


@router.get(
    "/relationships/as-of/{volume_id}",
    response_model=RelationshipListResponse,
)
async def list_relationships_as_of_volume(
    project_id: str,
    volume_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> RelationshipListResponse:
    """Return the relationship snapshot visible at ``volume_id``.

    A relationship is visible if its ``since_volume_id`` is null or
    comes before ``volume_id`` (by volume ``order_index``), and its
    ``until_volume_id`` is null or comes on/after ``volume_id``.
    Evolution entries whose ``volume_id`` is on/before the target
    volume are applied in order to override ``label``/``sentiment``/
    ``note`` on the returned response objects.
    """
    from app.models.project import Volume  # noqa: E402

    target = await db.get(Volume, volume_id)
    if target is None or str(target.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")
    target_idx = target.volume_idx

    vol_result = await db.execute(
        select(Volume.id, Volume.volume_idx).where(
            Volume.project_id == project_id
        )
    )
    vol_index: dict[str, int] = {
        str(vid): idx for vid, idx in vol_result.all()
    }

    result = await db.execute(
        select(Relationship).where(Relationship.project_id == project_id)
    )
    rels = list(result.scalars().all())

    visible: list[RelationshipResponse] = []
    for rel in rels:
        since_idx = (
            vol_index.get(str(rel.since_volume_id))
            if rel.since_volume_id
            else None
        )
        until_idx = (
            vol_index.get(str(rel.until_volume_id))
            if rel.until_volume_id
            else None
        )
        if since_idx is not None and since_idx > target_idx:
            continue
        if until_idx is not None and until_idx < target_idx:
            continue

        merged_label = rel.label
        merged_sentiment = rel.sentiment
        merged_note = rel.note
        for entry in rel.evolution_json or []:
            e_vid = entry.get("volume_id")
            e_idx = vol_index.get(str(e_vid)) if e_vid else None
            if e_idx is None or e_idx > target_idx:
                continue
            if entry.get("label") is not None:
                merged_label = entry["label"]
            if entry.get("sentiment") is not None:
                merged_sentiment = entry["sentiment"]
            if entry.get("note") is not None:
                merged_note = entry["note"]

        resp = RelationshipResponse.model_validate(rel)
        resp = resp.model_copy(
            update={
                "label": merged_label,
                "sentiment": merged_sentiment,
                "note": merged_note,
            }
        )
        visible.append(resp)

    return RelationshipListResponse(
        relationships=visible, total=len(visible)
    )
