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

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["settings"],
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

    update_data = body.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(character, field_name, value)

    await db.flush()
    await db.refresh(character)
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

    await db.delete(character)
    await db.flush()


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
    """Create a new world rule."""
    rule = WorldRule(
        project_id=project_id,
        category=body.category,
        rule_text=body.rule_text,
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return WorldRuleResponse.model_validate(rule)


@router.put("/world-rules/{rule_id}", response_model=WorldRuleResponse)
async def update_world_rule(
    project_id: UUID,
    rule_id: UUID,
    body: WorldRuleUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> WorldRuleResponse:
    """Update a world rule."""
    result = await db.execute(
        select(WorldRule).where(
            WorldRule.id == rule_id,
            WorldRule.project_id == project_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="World rule not found")

    update_data = body.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(rule, field_name, value)

    await db.flush()
    await db.refresh(rule)
    return WorldRuleResponse.model_validate(rule)


@router.delete("/world-rules/{rule_id}", status_code=204)
async def delete_world_rule(
    project_id: UUID,
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a world rule."""
    result = await db.execute(
        select(WorldRule).where(
            WorldRule.id == rule_id,
            WorldRule.project_id == project_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="World rule not found")

    await db.delete(rule)
    await db.flush()


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
    rel = Relationship(
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        rel_type=body.rel_type,
        label=body.label,
        note=body.note,
        sentiment=body.sentiment,
    )
    db.add(rel)
    await db.flush()
    await db.refresh(rel)
    return RelationshipResponse.model_validate(rel)


@router.post("/relationships/bulk", response_model=RelationshipBulkResponse, status_code=201)
async def bulk_create_relationships(
    project_id: str,
    body: RelationshipBulkRequest,
    db: AsyncSession = Depends(get_db),
) -> RelationshipBulkResponse:
    created = 0
    for item in body.items:
        rel = Relationship(
            project_id=project_id,
            source_id=item.source_id,
            target_id=item.target_id,
            rel_type=item.rel_type,
            label=item.label,
            note=item.note,
            sentiment=item.sentiment,
        )
        db.add(rel)
        created += 1
    await db.flush()
    return RelationshipBulkResponse(created=created)


@router.put("/relationships/{relationship_id}", response_model=RelationshipResponse)
async def update_relationship(
    project_id: str,
    relationship_id: str,
    body: RelationshipUpdate,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    rel = await db.get(Relationship, relationship_id)
    if rel is None or str(rel.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Relationship not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(rel, k, v)
    await db.flush()
    await db.refresh(rel)
    return RelationshipResponse.model_validate(rel)


@router.delete("/relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    project_id: str,
    relationship_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    rel = await db.get(Relationship, relationship_id)
    if rel is None or str(rel.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Relationship not found")
    await db.delete(rel)
    await db.flush()
