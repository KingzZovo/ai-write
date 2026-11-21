"""Pydantic v2 schemas for request/response validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.chapter_target_words import CHAPTER_DEFAULT_WORD_COUNT


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    title: str = Field(..., max_length=500)
    genre: str | None = Field(None, max_length=100)
    genre_profile_code: str | None = Field(None, max_length=64)
    premise: str | None = None
    settings_json: dict[str, Any] | None = None
    target_word_count: int | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, max_length=500)
    genre: str | None = Field(None, max_length=100)
    genre_profile_code: str | None = Field(None, max_length=64)
    premise: str | None = None
    settings_json: dict[str, Any] | None = None
    target_word_count: int | None = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    genre: str | None = None
    genre_profile_code: str | None = None
    premise: str | None = None
    settings_json: dict[str, Any] | None = None
    target_word_count: int = 3000000
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

class VolumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    title: str
    volume_idx: int
    summary: str | None = None
    target_word_count: int = 200000
    created_at: datetime


# ---------------------------------------------------------------------------
# Chapter
# ---------------------------------------------------------------------------

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
    target_word_count: int = CHAPTER_DEFAULT_WORD_COUNT
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------

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

class StyleProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_book: str | None = None
    config_json: dict[str, Any] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Foreshadow
# ---------------------------------------------------------------------------

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
    since_volume_id: UUID | None = None
    until_volume_id: UUID | None = None


class RelationshipUpdate(BaseModel):
    rel_type: str | None = Field(None, max_length=50)
    label: str | None = Field(None, max_length=200)
    note: str | None = None
    sentiment: str | None = Field(None, max_length=20)
    since_volume_id: UUID | None = None
    until_volume_id: UUID | None = None


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
    since_volume_id: UUID | None = None
    until_volume_id: UUID | None = None
    evolution_json: list[dict[str, Any]] = Field(default_factory=list)


class RelationshipListResponse(BaseModel):
    relationships: list[RelationshipResponse]
    total: int


class RelationshipBulkRequest(BaseModel):
    items: list[RelationshipCreate]
