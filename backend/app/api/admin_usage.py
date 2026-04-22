"""Admin endpoint: inspect and edit per-user monthly usage / quota.

Gate: only JWT subjects listed in env ``ADMIN_USERNAMES`` (comma separated)
may call these endpoints. When ``ADMIN_USERNAMES`` is unset, the gate falls
back to the single username ``admin``.

Routes:
  - GET  /api/admin/usage?user_id=<sub>&month=<YYYY-MM>
  - POST /api/admin/usage/quota   body: {user_id, month_ym?, quota_cents}

Both return the UsageQuota row as JSON.
"""

from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.usage_service import (
    current_month_ym,
    get_or_create_month,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_usernames() -> set[str]:
    raw = os.environ.get("ADMIN_USERNAMES", "admin")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _caller_username(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def _require_admin(request: Request) -> str:
    caller = _caller_username(request)
    if caller is None or caller not in _admin_usernames():
        raise HTTPException(status_code=403, detail="admin only")
    return caller


def _row_to_dict(row) -> dict:
    return {
        "id": int(row.id) if row.id is not None else None,
        "user_id": row.user_id,
        "month_ym": row.month_ym,
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "cost_cents": int(row.cost_cents or 0),
        "quota_cents": int(row.quota_cents or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class QuotaUpdate(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    month_ym: Optional[str] = Field(default=None, min_length=7, max_length=7)
    quota_cents: int = Field(ge=0)


@router.get("/usage")
async def get_usage(
    request: Request,
    user_id: str,
    month: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    _require_admin(request)
    month_ym = month or current_month_ym()
    row = await get_or_create_month(db, user_id=user_id, month_ym=month_ym)
    return _row_to_dict(row)


@router.post("/usage/quota")
async def set_quota(
    request: Request,
    body: QuotaUpdate,
    db: AsyncSession = Depends(get_db),
):
    _require_admin(request)
    row = await get_or_create_month(
        db,
        user_id=body.user_id,
        month_ym=body.month_ym or current_month_ym(),
    )
    row.quota_cents = int(body.quota_cents)
    await db.flush()
    await db.refresh(row)
    return _row_to_dict(row)
