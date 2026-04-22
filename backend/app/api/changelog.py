"""v0.9 settings change log query endpoints.

Exposes the ``settings_change_log`` table populated by
``app.services.change_log.record_change`` so the frontend timeline can
show how characters / world rules / relationships have evolved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.settings_change_log import SettingsChangeLog

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["changelog"],
)


class ChangeLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    actor_type: str
    actor_id: str | None = None
    target_type: str
    target_id: UUID | None = None
    action: str
    before_json: dict[str, Any] | list[Any] | None = None
    after_json: dict[str, Any] | list[Any] | None = None
    reason: str | None = None
    created_at: datetime


class ChangeLogResponse(BaseModel):
    entries: list[ChangeLogEntry]
    total: int
    has_more: bool


@router.get("/changelog", response_model=ChangeLogResponse)
async def list_changelog(
    project_id: UUID,
    actor_type: Literal["user", "agent", "critic", "system"] | None = Query(
        None, description="Filter by actor type"
    ),
    target_type: Literal["character", "world_rule", "relationship"]
    | None = Query(None, description="Filter by target type"),
    target_id: UUID | None = Query(
        None, description="Filter by a specific target entity id"
    ),
    action: Literal["create", "update", "delete"] | None = Query(
        None, description="Filter by action"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ChangeLogResponse:
    """Return the settings change log for a project, newest first."""
    stmt = select(SettingsChangeLog).where(
        SettingsChangeLog.project_id == project_id
    )
    if actor_type is not None:
        stmt = stmt.where(SettingsChangeLog.actor_type == actor_type)
    if target_type is not None:
        stmt = stmt.where(SettingsChangeLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(SettingsChangeLog.target_id == target_id)
    if action is not None:
        stmt = stmt.where(SettingsChangeLog.action == action)

    stmt = stmt.order_by(SettingsChangeLog.created_at.desc()).limit(
        limit + 1
    ).offset(offset)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ChangeLogResponse(
        entries=[ChangeLogEntry.model_validate(r) for r in rows],
        total=len(rows),
        has_more=has_more,
    )
