"""UsageService -- monthly per-user token/cost tracking and quota enforcement.

Row key is ``(user_id, month_ym)``. ``user_id`` is the JWT subject
(username string), not a UUID.

Public helpers:
  - ``current_month_ym()``             -> "YYYY-MM" in UTC
  - ``get_or_create_month(db, ...)``   -> UsageQuota row (creates if missing)
  - ``record_usage(db, ...)``          -> add tokens/cost to the current month
  - ``check_quota(db, ...)``           -> (allowed, row) tuple; allowed=False
                                          when ``quota_cents > 0`` and the
                                          current ``cost_cents`` has already
                                          exceeded it.

All helpers take an AsyncSession; the caller owns commit/rollback unless
``commit=True`` is passed.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_quota import UsageQuota

logger = logging.getLogger(__name__)


def _default_quota_cents() -> int:
    """Default monthly quota (cents) applied to newly-created rows.

    Controlled by env ``USAGE_DEFAULT_QUOTA_CENTS``. ``0`` means unlimited
    (the 402 interceptor treats ``quota_cents <= 0`` as 'no hard cap').
    """
    try:
        return int(os.environ.get("USAGE_DEFAULT_QUOTA_CENTS", "0"))
    except ValueError:
        return 0


def current_month_ym(now: datetime | None = None) -> str:
    """Return the current month as ``YYYY-MM`` (UTC)."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


async def get_or_create_month(
    db: AsyncSession,
    *,
    user_id: str,
    month_ym: str | None = None,
    default_quota_cents: int | None = None,
    commit: bool = False,
) -> UsageQuota:
    """Return the row for ``(user_id, month_ym)``, creating it if missing.

    The initial ``quota_cents`` is taken from ``default_quota_cents`` or from
    ``USAGE_DEFAULT_QUOTA_CENTS`` (env) -- ``0`` means unlimited.
    """
    month_ym = month_ym or current_month_ym()
    stmt = select(UsageQuota).where(
        UsageQuota.user_id == user_id,
        UsageQuota.month_ym == month_ym,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row

    row = UsageQuota(
        user_id=user_id,
        month_ym=month_ym,
        prompt_tokens=0,
        completion_tokens=0,
        cost_cents=0,
        quota_cents=int(
            _default_quota_cents() if default_quota_cents is None else default_quota_cents
        ),
    )
    db.add(row)
    try:
        await db.flush()
        await db.refresh(row)
    except Exception:
        # race: another worker inserted the same (user, month) -- roll back and reselect.
        await db.rollback()
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise
    if commit:
        await db.commit()
    return row


async def record_usage(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_cents: int = 0,
    month_ym: str | None = None,
    commit: bool = False,
) -> UsageQuota:
    """Increment the monthly counters for ``user_id`` and return the row.

    Negative deltas are clamped to 0 to avoid accidental refunds corrupting the
    audit trail. Call this *after* each successful LLM invocation.
    """
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cost_cents = max(0, int(cost_cents or 0))

    row = await get_or_create_month(db, user_id=user_id, month_ym=month_ym)
    row.prompt_tokens = int(row.prompt_tokens or 0) + prompt_tokens
    row.completion_tokens = int(row.completion_tokens or 0) + completion_tokens
    row.cost_cents = int(row.cost_cents or 0) + cost_cents
    row.updated_at = datetime.now(timezone.utc)
    try:
        await db.flush()
        await db.refresh(row)
    except Exception:
        await db.rollback()
        raise
    if commit:
        await db.commit()
    return row


async def check_quota(
    db: AsyncSession,
    *,
    user_id: str,
    month_ym: str | None = None,
) -> Tuple[bool, UsageQuota]:
    """Return ``(allowed, row)``.

    ``allowed`` is ``False`` when ``quota_cents > 0`` and ``cost_cents`` has
    already met or exceeded it. ``quota_cents <= 0`` means unlimited.
    Does not commit.
    """
    row = await get_or_create_month(db, user_id=user_id, month_ym=month_ym)
    quota = int(row.quota_cents or 0)
    used = int(row.cost_cents or 0)
    allowed = quota <= 0 or used < quota
    return allowed, row
