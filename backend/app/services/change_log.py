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
        if commit:
            await _invalidate_ctxpack(str(project_id))
        else:
            # 2026-11-17:调用方还没提交就失效缓存会留下竞态窗口 —— 并发的 ContextPack
            # 构建可能在这段窗口里用**旧**设定重建缓存并长期驻留,后续章节继续拿旧设定
            # 写作(正是本项目要防的漂移)。改为挂 after_commit 再失效。
            _invalidate_after_commit(db, str(project_id))

    return log


async def _invalidate_ctxpack(project_id: str) -> None:
    try:
        await ctxpack_cache.invalidate(project_id)
    except Exception as exc:  # never let cache failure break writes
        logger.warning("ctxpack invalidation after change_log failed: %s", exc)


def _invalidate_after_commit(db, project_id: str) -> None:
    """把失效推迟到本次事务真正提交之后;挂不上事件时退回立即失效。"""
    import asyncio

    from sqlalchemy import event

    sync_session = getattr(db, "sync_session", None)
    if sync_session is None:
        asyncio.ensure_future(_invalidate_ctxpack(project_id))
        return
    pending = getattr(sync_session, "_ctxpack_pending_invalidations", None)
    if pending is None:
        pending = set()
        sync_session._ctxpack_pending_invalidations = pending

        def _after_commit(session) -> None:  # sync hook: 只负责调度协程
            ids = getattr(session, "_ctxpack_pending_invalidations", set()) or set()
            session._ctxpack_pending_invalidations = set()
            for pid in ids:
                try:
                    asyncio.get_running_loop().create_task(_invalidate_ctxpack(pid))
                except RuntimeError:
                    logger.warning("ctxpack invalidation skipped (no running loop): %s", pid)

        def _after_rollback(session) -> None:
            session._ctxpack_pending_invalidations = set()

        try:
            event.listen(sync_session, "after_commit", _after_commit)
            event.listen(sync_session, "after_rollback", _after_rollback)
        except Exception as exc:  # pragma: no cover - 非标准 session 实现
            logger.warning("ctxpack invalidation hook unavailable (%s); invalidating now", exc)
            asyncio.ensure_future(_invalidate_ctxpack(project_id))
            return
    pending.add(project_id)
