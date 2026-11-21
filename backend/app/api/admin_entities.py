"""Admin endpoint: force entity materialization / extraction.

Why this exists:
- Entity writeback (Neo4j → Postgres) runs in the Celery worker process.
- Prometheus /metrics is served by the FastAPI backend process.
- Therefore, the backend cannot observe worker-only counters unless we also
  provide a way to execute materialization inside the backend process.

This module provides an admin-only endpoint to:
- materialize Neo4j Character/RELATES_TO into Postgres read models
- increment the backend-process metric `entity_pg_materialize_total`

Security:
- Requires JWT auth (same as other /api/* endpoints)
- Additionally gated by ADMIN_USERNAMES (same gate used by admin_usage)

Routes:
- POST /api/admin/entities/materialize  body: {project_id, chapter_idx?, caller?}

Note:
- This endpoint intentionally does NOT run LLM extraction. It only reads
  already-written Neo4j nodes/edges.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.admin_usage import _require_admin

router = APIRouter(prefix="/api/admin/entities", tags=["admin"])


class MaterializeRequest(BaseModel):
    project_id: str = Field(min_length=1)
    chapter_idx: Optional[int] = Field(default=None, ge=0)
    caller: str = Field(default="api.admin.entities.materialize", min_length=1)


@router.post("/materialize")
async def materialize_entities(request: Request, body: MaterializeRequest) -> dict:
    _require_admin(request)

    # We accept chapter_idx for logging/marker symmetry, but the current
    # materialization implementation reads all Character/RELATES_TO for the
    # project (not time-sliced).
    from app.tasks.entity_tasks import _materialize_entities_to_postgres

    try:
        result = await _materialize_entities_to_postgres(
            project_id=str(body.project_id),
            chapter_idx=int(body.chapter_idx or 0),
            caller=str(body.caller),
        )
        return {
            "status": "ok",
            "project_id": str(body.project_id),
            "chapter_idx": int(body.chapter_idx or 0),
            "result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

