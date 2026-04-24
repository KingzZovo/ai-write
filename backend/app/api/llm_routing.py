"""LLM routing matrix API (v1.4).

Read-only endpoint that exposes the current task_type -> endpoint/tier mapping
as loaded by the ModelRouter. Used by the frontend /llm-routing page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services.model_router import get_model_router_async

router = APIRouter(prefix="/api/llm-routing", tags=["llm-routing"])

VALID_TIERS = {"flagship", "standard", "small", "distill", "embedding"}


@router.get("/matrix")
async def get_routing_matrix(
    tier: str | None = Query(None, description="Optional tier filter"),
) -> dict[str, Any]:
    """Return current routing matrix: task_type -> endpoint + tier.

    Optional ?tier=flagship|standard|small|distill|embedding filters the rows.
    """
    router_ = await get_model_router_async()
    rows = router_.list_routes_matrix()
    if tier:
        if tier not in VALID_TIERS:
            return {"rows": [], "total": 0, "tier": tier, "error": "invalid tier"}
        rows = [r for r in rows if r["tier"] == tier]
    return {
        "rows": rows,
        "total": len(rows),
        "tier": tier,
    }
