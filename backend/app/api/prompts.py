"""Prompt Registry API — CRUD, versioning, and analytics for prompt assets."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services.prompt_registry import PromptRegistry
from app.services.prompt_recommendations import (
    TASK_TYPE_RECOMMENDATIONS,
    get_recommendation,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


# ---------------------------------------------------------------------------
# v1.5.0 B2 — recommendation mismatch soft-guard
# ---------------------------------------------------------------------------
# When a prompt is saved with an endpoint whose kind/tier disagrees with the
# task_type's curated recommendation, return 409 + structured payload so the
# frontend can render a confirm dialog. Caller can re-submit with
# `?confirm_mismatch=true` to bypass. This is a soft warning — it never
# blocks saves once the user has confirmed. Motivation: v1.4.x 401 incident
# was caused by silently binding a generation prompt to an embedding
# endpoint with no UI feedback.


def _endpoint_kind_from_tier(tier: str | None) -> str:
    """Map an llm_endpoints.tier value to a recommendation "kind"."""
    return "embedding" if (tier or "").lower() == "embedding" else "chat"


async def _check_recommendation_mismatch(
    *,
    db: AsyncSession,
    task_type: str,
    endpoint_id: UUID | None,
    model_tier: str | None,
) -> dict | None:
    """Return mismatch payload, or None if the binding is fine / unknown.

    A mismatch is reported when ALL of the following hold:
    - task_type has an explicit entry in TASK_TYPE_RECOMMENDATIONS
      (avoids warning on unknown task_types where the default is just a guess);
    - endpoint_id is set (we can't compare against an unbound prompt);
    - the endpoint's effective kind/tier disagrees with the recommendation.

    Effective tier = explicit prompt model_tier override, else endpoint.tier.
    Effective kind = embedding if effective tier == "embedding" else chat.
    """
    if endpoint_id is None:
        return None
    if task_type not in TASK_TYPE_RECOMMENDATIONS:
        return None

    endpoint = await db.get(LLMEndpoint, str(endpoint_id))
    if endpoint is None:
        return None

    rec = get_recommendation(task_type)
    rec_kind = rec["kind"]
    rec_tier = rec["tier"]

    ep_tier = (endpoint.tier or "").lower() or None
    eff_tier = (model_tier or ep_tier or "standard").lower()
    eff_kind = _endpoint_kind_from_tier(eff_tier)

    kind_mismatch = rec_kind != eff_kind
    tier_mismatch = rec_tier != eff_tier

    if not kind_mismatch and not tier_mismatch:
        return None

    return {
        "code": "recommendation_mismatch",
        "task_type": task_type,
        "recommended_kind": rec_kind,
        "recommended_tier": rec_tier,
        "recommendation_reason": rec["reason"],
        "current_kind": eff_kind,
        "current_tier": eff_tier,
        "endpoint_id": str(endpoint.id),
        "endpoint_name": endpoint.name,
        "endpoint_tier": ep_tier,
        "prompt_model_tier": model_tier,
        "kind_mismatch": kind_mismatch,
        "tier_mismatch": tier_mismatch,
    }


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
    # v1.4 — preferred LLM tier
    model_tier: str | None = None
    success_count: int
    fail_count: int
    avg_score: int
    created_at: Any
    updated_at: Any
    # v1.4.1 — per-task-type endpoint recommendation (chat vs embedding,
    # and when chat, a suggested tier). Populated by the list endpoint;
    # harmless default for single-prompt reads.
    recommendation: dict | None = None


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
    # v1.4 — preferred LLM tier (flagship|standard|small|distill|embedding)
    model_tier: str | None = Field(
        default=None,
        pattern=r"^(flagship|standard|small|distill|embedding)$",
    )


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
    # v1.4 — preferred LLM tier (flagship|standard|small|distill|embedding)
    model_tier: str | None = Field(
        default=None,
        pattern=r"^(flagship|standard|small|distill|embedding)$",
    )


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
    results: list[PromptResponse] = []
    for a in assets:
        resp = PromptResponse.model_validate(a)
        # v1.4.1 — attach endpoint recommendation (kind/tier/reason) so the
        # UI can hint which endpoint type to bind per prompt.
        resp.recommendation = get_recommendation(a.task_type)
        results.append(resp)
    # v1.5.0 C3: list_prompts auto-seeds new built-in task_types on first
    # call. If anything was seeded, drop the snapshot cache so the new rows
    # are picked up immediately rather than after TTL.
    if seeded:
        from app.services.prompt_cache import invalidate as _pc_invalidate
        _pc_invalidate()
    return results


@router.post("", response_model=PromptResponse, status_code=201)
async def create_prompt(
    body: PromptCreate,
    db: AsyncSession = Depends(get_db),
    confirm_mismatch: bool = Query(
        False,
        description="v1.5.0 B2 — set true to bypass the recommendation mismatch soft-guard.",
    ),
) -> PromptResponse:
    """Create a new prompt asset (new version for existing task_type)."""
    # v1.5.0 B2 — soft-guard: warn (HTTP 409) when the bound endpoint's
    # kind/tier disagrees with the task_type recommendation. UI re-submits
    # with confirm_mismatch=true after operator confirmation.
    if not confirm_mismatch:
        mismatch = await _check_recommendation_mismatch(
            db=db,
            task_type=body.task_type,
            endpoint_id=body.endpoint_id,
            model_tier=body.model_tier,
        )
        if mismatch is not None:
            raise HTTPException(status_code=409, detail=mismatch)

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
        model_tier=body.model_tier,
    )
    db.add(asset)
    await db.flush()
    await db.refresh(asset)
    from app.services.model_router import reset_model_router
    reset_model_router()
    # v1.5.0 C3: drop snapshot for this task_type so subsequent runner calls
    # re-fetch (and so the previously active version's stale row is not
    # returned by the cache after the in-place is_active=0 update).
    from app.services.prompt_cache import invalidate as _pc_invalidate
    _pc_invalidate(body.task_type)
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
    resp = PromptResponse.model_validate(asset)
    resp.recommendation = get_recommendation(asset.task_type)
    return resp


@router.put("/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: UUID,
    body: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    confirm_mismatch: bool = Query(
        False,
        description="v1.5.0 B2 — set true to bypass the recommendation mismatch soft-guard.",
    ),
) -> PromptResponse:
    """Update a prompt asset (edits in place, use POST for new version)."""
    asset = await db.get(PromptAsset, str(prompt_id))
    if not asset:
        raise HTTPException(status_code=404, detail="Prompt 不存在")

    # v1.5.0 B2 — soft-guard mismatch check using the post-edit endpoint /
    # model_tier (so the operator is warned about the value they are about
    # to write, not the stale value).
    if not confirm_mismatch:
        update_data = body.model_dump(exclude_unset=True)
        next_endpoint_id = (
            update_data["endpoint_id"] if "endpoint_id" in update_data
            else asset.endpoint_id
        )
        next_model_tier = (
            update_data["model_tier"] if "model_tier" in update_data
            else asset.model_tier
        )
        mismatch = await _check_recommendation_mismatch(
            db=db,
            task_type=asset.task_type,
            endpoint_id=next_endpoint_id,
            model_tier=next_model_tier,
        )
        if mismatch is not None:
            raise HTTPException(status_code=409, detail=mismatch)

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)

    await db.flush()
    await db.refresh(asset)
    # v0.5: mutations to routing fields must re-seed ModelRouter cache
    from app.services.model_router import reset_model_router
    reset_model_router()
    # v1.5.0 C3: drop snapshot for this task_type so the next runner call
    # picks up the edit instead of serving stale system_prompt / endpoint /
    # tier from the in-process cache.
    from app.services.prompt_cache import invalidate as _pc_invalidate
    _pc_invalidate(asset.task_type)
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
    # v1.5.0 C3: capture task_type before delete so we can invalidate after.
    deleted_task_type = asset.task_type
    await db.delete(asset)
    from app.services.prompt_cache import invalidate as _pc_invalidate
    _pc_invalidate(deleted_task_type)


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
