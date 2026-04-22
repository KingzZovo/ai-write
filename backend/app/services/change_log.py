"""v0.9: Settings change log service.

Centralises writes to the ``settings_change_log`` table and always emits a
Context-Pack invalidation as a side-effect. All authoring endpoints that
mutate characters / world_rules / relationships should go through this.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings_change_log import SettingsChangeLog
from app.services import ctxpack_cache

logger = logging.getLogger(__name__)

VALID_TARGETS = {"character", "world_rule", "relationship"}
VALID_ACTIONS = {"create", "update", "delete"}
VALID_ACTORS = {"user", "agent", "critic", "system"}


def _coerce_json(value: Any) -> Any:
    """Make arbitrary values JSON-serialisable for audit storage."""
    if value is None:
        return {}
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    # ORM objects etc. — shallow-serialise via __dict__ then strip private keys
    try:
        raw = getattr(value, "__dict__", None) or {}
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, (str, int, float, bool, dict, list, type(None)))}
    except Exception:
        return {"repr": repr(value)}


async def record_change(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | str,
    target_type: str,
    target_id: uuid.UUID | str | None,
    action: str,
    before: Any = None,
    after: Any = None,
    actor_type: str = "user",
    actor_id: str | None = None,
    reason: str | None = None,
    invalidate_ctxpack: bool = True,
    commit: bool = False,
) -> SettingsChangeLog:
    """Insert one change-log row and best-effort invalidate the project's ctxpack cache.

    - Pass ``commit=False`` when the caller owns the transaction; pass
      ``commit=True`` for fire-and-forget writes from helpers that don't
      have an outer transaction.
    - ``before`` / ``after`` are coerced via :func:`_coerce_json` so ORM
      snapshots are accepted.
    - Invalidation failures are logged but never raise.
    """
    if target_type not in VALID_TARGETS:
        raise ValueError(f"unsupported target_type: {target_type!r}")
    if action not in VALID_ACTIONS:
        raise ValueError(f"unsupported action: {action!r}")
    if actor_type not in VALID_ACTORS:
        raise ValueError(f"unsupported actor_type: {actor_type!r}")

    log = SettingsChangeLog(
        id=uuid.uuid4(),
        project_id=project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id)),
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id if (target_id is None or isinstance(target_id, uuid.UUID)) else uuid.UUID(str(target_id)),
        action=action,
        before_json=_coerce_json(before),
        after_json=_coerce_json(after),
        reason=reason,
    )
    db.add(log)
    if commit:
        try:
            await db.commit()
            await db.refresh(log)
        except Exception:
            await db.rollback()
            raise

    if invalidate_ctxpack:
        try:
            await ctxpack_cache.invalidate(str(project_id))
        except Exception as exc:  # never let cache failure break writes
            logger.warning("ctxpack invalidation after change_log failed: %s", exc)

    return log
