"""Prompt Registry API — CRUD, versioning, and analytics for prompt assets."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.prompt import PromptAsset
from app.services.prompt_registry import PromptRegistry

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_type: str
    name: str
    name_en: str = ""
    description: str
    description_en: str = ""
    mode: str
    system_prompt: str
    user_template: str
    output_schema: dict | None
    context_policy: str
    version: int
    is_active: int
    endpoint_id: UUID | None = None
    model_name: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    category: str = "Core"
    order: int = 0
    always_enabled: int = 0
    success_count: int
    fail_count: int
    avg_score: int
    created_at: Any
    updated_at: Any


class PromptCreate(BaseModel):
    task_type: str
    name: str
    name_en: str = ""
    description: str = ""
    description_en: str = ""
    mode: str = "text"
    system_prompt: str
    user_template: str = ""
    output_schema: dict | None = None
    context_policy: str = "default"
    endpoint_id: UUID | None = None
    model_name: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    category: str = "Core"
    order: int = 0
    always_enabled: int = 0


class PromptUpdate(BaseModel):
    name: str | None = None
    name_en: str | None = None
    description: str | None = None
    description_en: str | None = None
    system_prompt: str | None = None
    user_template: str | None = None
    output_schema: dict | None = None
    context_policy: str | None = None
    is_active: int | None = None
    endpoint_id: UUID | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    category: str | None = None
    order: int | None = None
    always_enabled: int | None = None


@router.get("", response_model=list[PromptResponse])
async def list_prompts(
    db: AsyncSession = Depends(get_db),
) -> list[PromptResponse]:
    """List all prompt assets. Auto-seeds built-in prompts on first call."""
    registry = PromptRegistry(db)
    seeded = await registry.seed_builtins()
    if seeded:
        await db.flush()

    assets = await registry.get_all()
    return [PromptResponse.model_validate(a) for a in assets]


@router.post("", response_model=PromptResponse, status_code=201)
async def create_prompt(
    body: PromptCreate,
    db: AsyncSession = Depends(get_db),
) -> PromptResponse:
    """Create a new prompt asset (new version for existing task_type)."""
    # Check existing version for this task_type
    result = await db.execute(
        select(PromptAsset)
        .where(PromptAsset.task_type == body.task_type)
        .order_by(PromptAsset.version.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    new_version = (existing.version + 1) if existing else 1

    # Deactivate old versions
    if existing:
        from sqlalchemy import update
        await db.execute(
            update(PromptAsset)
            .where(PromptAsset.task_type == body.task_type)
            .values(is_active=0)
        )

    asset = PromptAsset(
        task_type=body.task_type,
        name=body.name,
        name_en=body.name_en,
        description=body.description,
        description_en=body.description_en,
        mode=body.mode,
        system_prompt=body.system_prompt,
        user_template=body.user_template,
        output_schema=body.output_schema,
        context_policy=body.context_policy,
        version=new_version,
        is_active=1,
        endpoint_id=body.endpoint_id,
        model_name=body.model_name,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        category=body.category,
        order=body.order,
        always_enabled=body.always_enabled,
    )
    db.add(asset)
    await db.flush()
    await db.refresh(asset)
    from app.services.model_router import reset_model_router
    reset_model_router()
    return PromptResponse.model_validate(asset)


@router.get("/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> PromptResponse:
    """Get a prompt asset by ID."""
    asset = await db.get(PromptAsset, str(prompt_id))
    if not asset:
        raise HTTPException(status_code=404, detail="Prompt 不存在")
    return PromptResponse.model_validate(asset)


@router.put("/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: UUID,
    body: PromptUpdate,
    db: AsyncSession = Depends(get_db),
) -> PromptResponse:
    """Update a prompt asset (edits in place, use POST for new version)."""
    asset = await db.get(PromptAsset, str(prompt_id))
    if not asset:
        raise HTTPException(status_code=404, detail="Prompt 不存在")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)

    await db.flush()
    await db.refresh(asset)
    # v0.5: mutations to routing fields must re-seed ModelRouter cache
    from app.services.model_router import reset_model_router
    reset_model_router()
    return PromptResponse.model_validate(asset)


@router.delete("/{prompt_id}", status_code=204)
async def delete_prompt(
    prompt_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a prompt asset."""
    asset = await db.get(PromptAsset, str(prompt_id))
    if not asset:
        raise HTTPException(status_code=404, detail="Prompt 不存在")
    await db.delete(asset)


@router.get("/stats/summary")
async def prompt_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get usage stats for all prompt assets."""
    result = await db.execute(
        select(PromptAsset).where(PromptAsset.is_active == 1)
    )
    assets = result.scalars().all()
    return {
        "total": len(assets),
        "stats": [
            {
                "task_type": a.task_type,
                "name": a.name,
                "version": a.version,
                "success_count": a.success_count,
                "fail_count": a.fail_count,
                "success_rate": round(a.success_count / max(a.success_count + a.fail_count, 1) * 100, 1),
            }
            for a in assets
        ],
    }
