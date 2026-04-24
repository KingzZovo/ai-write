"""LLM routing matrix API (v1.4).

Read-only endpoint that exposes the effective routing for every
``(task_type, mode)`` PromptAsset joined with its bound LLMEndpoint, and
applies the three-level tier fallback defined in v1.4:

    prompt.model_tier ≫ endpoint.tier ≫ "standard"

The response shape matches the ``MatrixRow`` interface rendered by the
frontend ``/llm-routing`` page (chunk-14). Prior to chunk-17 the backend
only returned a minimal ``{task_type, endpoint_id, endpoint_name, model,
tier, temperature, max_tokens}`` projection, which the frontend could not
display correctly — this module fixes that v1.4 tail.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services.model_router import (
    VALID_TIERS,
    compute_effective_tier,
    is_valid_tier,
)

router = APIRouter(prefix="/api/llm-routing", tags=["llm-routing"])


def _build_row(
    asset: PromptAsset, endpoint: LLMEndpoint | None
) -> dict[str, Any]:
    """Compose a single MatrixRow dict for the frontend.

    ``overridden`` flags rows where the prompt pins an explicit tier that
    differs from the endpoint's declared tier — useful as an audit signal
    on the admin page.
    """
    endpoint_tier = (
        getattr(endpoint, "tier", None) if endpoint is not None else None
    )
    prompt_tier = getattr(asset, "model_tier", None)
    effective = compute_effective_tier(prompt_tier, endpoint_tier)
    overridden = bool(
        is_valid_tier(prompt_tier)
        and endpoint_tier
        and prompt_tier != endpoint_tier
    )
    return {
        "task_type": asset.task_type,
        "mode": asset.mode or "text",
        "prompt_id": str(asset.id),
        "prompt_name": asset.name,
        "endpoint_id": (
            str(endpoint.id) if endpoint is not None else None
        ),
        "endpoint_name": (
            endpoint.name if endpoint is not None else None
        ),
        "endpoint_tier": endpoint_tier,
        "model_name": asset.model_name or None,
        "model_tier": prompt_tier,
        "effective_tier": effective,
        "overridden": overridden,
    }


@router.get("/matrix")
async def get_routing_matrix(
    tier: str | None = Query(
        None,
        description=(
            "Optional tier filter. One of "
            "flagship|standard|small|distill|embedding."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the effective routing matrix for every active prompt.

    - One row per active ``PromptAsset`` (keyed by ``(task_type, mode)``).
    - ``effective_tier`` follows the v1.4 three-level fallback.
    - ``?tier=<enum>`` filters rows by their ``effective_tier``.
    - An unknown tier returns a 200 response with ``error: "invalid tier"``
      and ``total: 0`` instead of 5xx-ing — matches the contract documented
      in RELEASE_NOTES_v1.4.md.
    """
    if tier and tier not in VALID_TIERS:
        return {
            "rows": [],
            "total": 0,
            "tier": tier,
            "error": "invalid tier",
        }

    assets_result = await db.execute(
        select(PromptAsset).where(PromptAsset.is_active == 1)
    )
    assets = list(assets_result.scalars().all())

    endpoint_ids = {
        a.endpoint_id for a in assets if a.endpoint_id is not None
    }
    endpoints_by_id: dict[UUID, LLMEndpoint] = {}
    if endpoint_ids:
        eps_result = await db.execute(
            select(LLMEndpoint).where(LLMEndpoint.id.in_(endpoint_ids))
        )
        endpoints_by_id = {
            ep.id: ep for ep in eps_result.scalars().all()
        }

    rows = [
        _build_row(
            a,
            endpoints_by_id.get(a.endpoint_id)
            if a.endpoint_id is not None
            else None,
        )
        for a in assets
    ]
    if tier:
        rows = [r for r in rows if r["effective_tier"] == tier]
    rows.sort(key=lambda r: (r["task_type"], r["mode"]))
    return {
        "rows": rows,
        "total": len(rows),
        "tier": tier,
    }
