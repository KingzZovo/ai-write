"""Pydantic v2 schemas for request/response validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    title: str = Field(..., max_length=500)
    genre: str | None = Field(None, max_length=100)
    premise: str | None = None
    settings_json: dict[str, Any] | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, max_length=500)
    genre: str | None = Field(None, max_length=100)
    premise: str | None = None
    settings_json: dict[str, Any] | None = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    genre: str | None = None
    premise: str | None = None
    settings_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

class VolumeCreate(BaseModel):
    title: str = Field(..., max_length=500)
    volume_idx: int
    summary: str | None = None


class VolumeUpdate(BaseModel):
    title: str | None = Field(None, max_length=500)
    volume_idx: int | None = None
    summary: str | None = None


class VolumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    title: str
    volume_idx: int
    summary: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Chapter
# ---------------------------------------------------------------------------

class ChapterCreate(BaseModel):
    title: str = Field(..., max_length=500)
    chapter_idx: int
    outline_json: dict[str, Any] | None = None
    content_text: str | None = None
    status: str | None = Field("draft", max_length=20)
    summary: str | None = None


class ChapterUpdate(BaseModel):
    title: str | None = Field(None, max_length=500)
    chapter_idx: int | None = None
    outline_json: dict[str, Any] | None = None
    content_text: str | None = None
    word_count: int | None = None
    status: str | None = Field(None, max_length=20)
    summary: str | None = None
    target_words: int | None = None


class ChapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    volume_id: UUID
    title: str
    chapter_idx: int
    outline_json: dict[str, Any] | None = None
    content_text: str | None = None
    word_count: int
    status: str
    summary: str | None = None
    target_words: int | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------

class OutlineCreate(BaseModel):
    level: str = Field(..., max_length=20)
    parent_id: UUID | None = None
    content_json: dict[str, Any] | None = None
    version: int | None = 1
    is_confirmed: int | None = 0


class OutlineUpdate(BaseModel):
    level: str | None = Field(None, max_length=20)
    parent_id: UUID | None = None
    content_json: dict[str, Any] | None = None
    version: int | None = None
    is_confirmed: int | None = None


class OutlineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    level: str
    parent_id: UUID | None = None
    content_json: dict[str, Any] | None = None
    version: int
    is_confirmed: int
    created_at: datetime


# ---------------------------------------------------------------------------
# Character
# ---------------------------------------------------------------------------

class CharacterCreate(BaseModel):
    name: str = Field(..., max_length=200)
    profile_json: dict[str, Any] | None = None


class CharacterUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    profile_json: dict[str, Any] | None = None


class CharacterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    profile_json: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# WorldRule
# ---------------------------------------------------------------------------

class WorldRuleCreate(BaseModel):
    category: str = Field(..., max_length=100)
    rule_text: str


class WorldRuleUpdate(BaseModel):
    category: str | None = Field(None, max_length=100)
    rule_text: str | None = None


class WorldRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    category: str
    rule_text: str
    created_at: datetime


# ---------------------------------------------------------------------------
# StyleProfile
# ---------------------------------------------------------------------------

class StyleProfileCreate(BaseModel):
    name: str = Field(..., max_length=200)
    source_book: str | None = Field(None, max_length=500)
    config_json: dict[str, Any] | None = None


class StyleProfileUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    source_book: str | None = Field(None, max_length=500)
    config_json: dict[str, Any] | None = None


class StyleProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_book: str | None = None
    config_json: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

class ModelConfigCreate(BaseModel):
    task_type: str = Field(..., max_length=50)
    provider: str = Field(..., max_length=50)
    model_name: str = Field(..., max_length=200)
    params_json: dict[str, Any] | None = None
    is_active: int | None = 1


class ModelConfigUpdate(BaseModel):
    task_type: str | None = Field(None, max_length=50)
    provider: str | None = Field(None, max_length=50)
    model_name: str | None = Field(None, max_length=200)
    params_json: dict[str, Any] | None = None
    is_active: int | None = None


class ModelConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_type: str
    provider: str
    model_name: str
    params_json: dict[str, Any] | None = None
    is_active: int


# ---------------------------------------------------------------------------
# Foreshadow
# ---------------------------------------------------------------------------

class ForeshadowCreate(BaseModel):
    type: str = Field(..., max_length=20)
    description: str
    planted_chapter: int
    resolve_conditions_json: list[Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float | None = 0.0
    status: str | None = Field("planted", max_length=20)


class ForeshadowUpdate(BaseModel):
    type: str | None = Field(None, max_length=20)
    description: str | None = None
    planted_chapter: int | None = None
    resolve_conditions_json: list[Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float | None = None
    status: str | None = Field(None, max_length=20)
    resolved_chapter: int | None = None


class ForeshadowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    type: str
    description: str
    planted_chapter: int
    resolve_conditions_json: list[Any] | None = None
    resolution_blueprint_json: dict[str, Any] | None = None
    narrative_proximity: float
    status: str
    resolved_chapter: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# VolumeSummary
# ---------------------------------------------------------------------------

class VolumeSummaryCreate(BaseModel):
    volume_id: UUID
    summary_text: str
    character_snapshot_json: dict[str, Any] | None = None
    plot_progress_json: dict[str, Any] | None = None


class VolumeSummaryUpdate(BaseModel):
    summary_text: str | None = None
    character_snapshot_json: dict[str, Any] | None = None
    plot_progress_json: dict[str, Any] | None = None


class VolumeSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    volume_id: UUID
    summary_text: str
    character_snapshot_json: dict[str, Any] | None = None
    plot_progress_json: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------


class RelationshipCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    rel_type: str = Field(..., max_length=50)
    label: str = Field(default="", max_length=200)
    note: str = ""
    sentiment: str = Field(default="neutral", max_length=20)


class RelationshipUpdate(BaseModel):
    rel_type: str | None = Field(None, max_length=50)
    label: str | None = Field(None, max_length=200)
    note: str | None = None
    sentiment: str | None = Field(None, max_length=20)


class RelationshipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    source_id: UUID
    target_id: UUID
    rel_type: str
    label: str
    note: str
    sentiment: str
    created_at: datetime


class RelationshipListResponse(BaseModel):
    relationships: list[RelationshipResponse]
    total: int


class RelationshipBulkRequest(BaseModel):
    items: list[RelationshipCreate]
