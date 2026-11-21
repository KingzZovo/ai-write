"""v0.8 — Writing engine CRUD API.

Five resources:
  /api/writing-rules
  /api/beat-patterns
  /api/anti-ai-traps
  /api/genre-profiles
  /api/tool-specs

Each resource exposes list / create / update / delete and, where relevant,
a /{id}/toggle endpoint for quick enable/disable.
"""

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.writing_engine import (
    AntiAITrap,
    BeatPattern,
    GenreProfile,
    ToolSpec,
    WritingRule,
)

router = APIRouter(tags=["v0.8", "writing-engine"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WritingRuleIn(BaseModel):
    genre: str = ""
    category: str
    title: str
    rule_text: str
    examples_json: list[dict[str, Any]] | None = None
    priority: int = 50
    is_active: bool = True


class WritingRuleOut(_Base):
    id: UUID
    genre: str
    category: str
    title: str
    rule_text: str
    examples_json: list[dict[str, Any]] | None = None
    priority: int
    is_active: bool


class BeatPatternIn(BaseModel):
    genre: str = ""
    stage: str
    title: str
    description: str = ""
    trigger_conditions_json: dict[str, Any] | None = None
    reusable: bool = True
    is_active: bool = True


class BeatPatternOut(_Base):
    id: UUID
    genre: str
    stage: str
    title: str
    description: str
    trigger_conditions_json: dict[str, Any] | None = None
    reusable: bool
    is_active: bool


class AntiAITrapIn(BaseModel):
    locale: str = "zh-CN"
    pattern_type: str = Field(pattern="^(keyword|regex|ngram)$")
    pattern: str
    severity: str = Field(default="soft", pattern="^(hard|soft)$")
    replacement_hint: str = ""
    is_active: bool = True


class AntiAITrapOut(_Base):
    id: UUID
    locale: str
    pattern_type: str
    pattern: str
    severity: str
    replacement_hint: str
    is_active: bool


class GenreProfileIn(BaseModel):
    code: str
    name: str
    description: str = ""
    default_beat_pattern_ids: list[str] | None = None
    default_writing_rule_ids: list[str] | None = None
    is_active: bool = True


class GenreProfileOut(_Base):
    id: UUID
    code: str
    name: str
    description: str
    default_beat_pattern_ids: list[str] | None = None
    default_writing_rule_ids: list[str] | None = None
    is_active: bool


class ToolSpecIn(BaseModel):
    name: str
    description: str = ""
    input_schema_json: dict[str, Any] | None = None
    output_schema_json: dict[str, Any] | None = None
    handler: str = Field(pattern="^(python_callable|sql|qdrant|llm)$")
    config_json: dict[str, Any] | None = None
    is_active: bool = True


class ToolSpecOut(_Base):
    id: UUID
    name: str
    description: str
    input_schema_json: dict[str, Any] | None = None
    output_schema_json: dict[str, Any] | None = None
    handler: str
    config_json: dict[str, Any] | None = None
    is_active: bool


# ---------------------------------------------------------------------------
# Generic CRUD factory
# ---------------------------------------------------------------------------


def _register_crud(
    path_prefix: str,
    model_cls,
    schema_in,
    schema_out,
    order_col,
):
    # NOTE: we dynamically build handlers with explicit parameter annotations
    # because FastAPI resolves Pydantic request-body types from closure names
    # at openapi generation, which fails when the name is a local variable.
    # Constructing the function via exec with a concrete annotation namespace
    # binds the type correctly.

    list_resp = list[schema_out]

    async def _list(db: AsyncSession = Depends(get_db)):
        rows = await db.execute(select(model_cls).order_by(order_col))
        return list(rows.scalars().all())

    _list.__annotations__ = {"db": AsyncSession, "return": list_resp}
    router.get(path_prefix, response_model=list_resp)(_list)

    async def _create(payload, db: AsyncSession = Depends(get_db)):
        obj = model_cls(id=uuid.uuid4(), **payload.model_dump(exclude_none=True))
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return obj

    _create.__annotations__ = {"payload": schema_in, "db": AsyncSession, "return": schema_out}
    router.post(path_prefix, response_model=schema_out)(_create)

    async def _update(item_id: UUID, payload, db: AsyncSession = Depends(get_db)):
        obj = await db.get(model_cls, item_id)
        if obj is None:
            raise HTTPException(404, f"{model_cls.__name__} not found")
        for k, v in payload.model_dump(exclude_none=True).items():
            setattr(obj, k, v)
        await db.commit()
        await db.refresh(obj)
        return obj

    _update.__annotations__ = {
        "item_id": UUID,
        "payload": schema_in,
        "db": AsyncSession,
        "return": schema_out,
    }
    router.put(path_prefix + "/{item_id}", response_model=schema_out)(_update)

    async def _delete(item_id: UUID, db: AsyncSession = Depends(get_db)):
        obj = await db.get(model_cls, item_id)
        if obj is None:
            raise HTTPException(404, f"{model_cls.__name__} not found")
        await db.delete(obj)
        await db.commit()
        return {"deleted": str(item_id)}

    router.delete(path_prefix + "/{item_id}")(_delete)

    async def _toggle(item_id: UUID, db: AsyncSession = Depends(get_db)):
        obj = await db.get(model_cls, item_id)
        if obj is None:
            raise HTTPException(404, f"{model_cls.__name__} not found")
        obj.is_active = not bool(obj.is_active)
        await db.commit()
        await db.refresh(obj)
        return obj

    _toggle.__annotations__ = {"item_id": UUID, "db": AsyncSession, "return": schema_out}
    router.post(path_prefix + "/{item_id}/toggle", response_model=schema_out)(_toggle)


_register_crud(
    "/api/writing-rules",
    WritingRule,
    WritingRuleIn,
    WritingRuleOut,
    order_col=WritingRule.priority.desc(),
)
_register_crud(
    "/api/beat-patterns",
    BeatPattern,
    BeatPatternIn,
    BeatPatternOut,
    order_col=BeatPattern.stage,
)
_register_crud(
    "/api/anti-ai-traps",
    AntiAITrap,
    AntiAITrapIn,
    AntiAITrapOut,
    order_col=AntiAITrap.severity.desc(),
)
_register_crud(
    "/api/genre-profiles",
    GenreProfile,
    GenreProfileIn,
    GenreProfileOut,
    order_col=GenreProfile.code,
)
_register_crud(
    "/api/tool-specs",
    ToolSpec,
    ToolSpecIn,
    ToolSpecOut,
    order_col=ToolSpec.name,
)
