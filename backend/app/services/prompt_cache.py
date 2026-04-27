"""Process-local cache layer for PromptAsset reads + buffered counter writes.

v1.5.0 C3 — collapses the per-LLM-call DB cost on the prompt registry path:

  Before C3, every ``run_text_prompt`` / ``stream_text_prompt`` call issued
  three statements against ``prompt_assets``:
    1. ``resolve_route``  -> SELECT (1 row)
    2. ``resolve_tier``   -> SELECT (same 1 row, redundantly)
    3. ``track_result``   -> UPDATE prompt_assets SET success_count = ... + 1

  The UPDATE in step 3 also caused the C2 deadlock: it ran on the outer
  request-scoped ``db`` session that was still idle-in-transaction holding
  row locks while the revise scene_writer (in a fresh session) tried to
  UPDATE the same row.

  This module gives us:
    - A read-through TTL snapshot cache keyed by ``task_type``. Snapshots are
      plain frozen dataclasses (NOT ORM instances), so they survive across
      sessions and are safe to share between requests.
    - A counter buffer that batches success/fail increments in memory and
      flushes them periodically using a FRESH session from
      ``async_session_factory()``. Critically, this means counter UPDATEs no
      longer share a transaction with the request session, eliminating the
      C2-class deadlock by construction.
    - Explicit ``invalidate(task_type)`` so the /prompts CRUD endpoints can
      drop stale snapshots after admin edits.

  The cache is process-local. With multiple uvicorn workers each worker has
  its own copy; that's fine because prompt edits via the admin UI happen
  rarely and the TTL bounds staleness.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (override in tests via monkeypatch on the module attributes).
# ---------------------------------------------------------------------------

# How long a positive snapshot stays warm before we re-fetch from DB.
TTL_SECONDS: float = 300.0
# How long a *negative* snapshot (missing task_type) is remembered.
NEG_TTL_SECONDS: float = 30.0
# Background flush cadence for buffered counters.
FLUSH_INTERVAL_SECONDS: float = 30.0


@dataclass(frozen=True)
class PromptAssetSnapshot:
    """Detached, read-only view of a PromptAsset row.

    Holds exactly the fields ``resolve_route`` / ``resolve_tier`` /
    ``run_text_prompt`` / ``run_structured_prompt`` need. Adding new fields
    here is cheap; removing fields is a breaking change for any caller that
    ``getattr``s on the snapshot.
    """

    id: UUID
    task_type: str
    name: str
    mode: str
    system_prompt: str
    endpoint_id: Optional[UUID]
    model_name: str
    temperature: float
    max_tokens: int
    model_tier: Optional[str]
    output_schema: Any
    user_template: str


# task_type -> (snapshot_or_None, expiry_epoch_seconds)
# A None snapshot is a negative cache entry meaning "DB has no row for this
# task_type"; it expires faster (NEG_TTL_SECONDS) so newly created prompts
# are picked up quickly.
_snap_cache: dict[str, tuple[Optional[PromptAssetSnapshot], float]] = {}
_snap_lock = asyncio.Lock()

# Buffered counter state: asset_id (str) -> pending count.
_pending_success: dict[str, int] = {}
_pending_fail: dict[str, int] = {}
_pending_lock = asyncio.Lock()

# Background flusher task handle. None until first scheduling.
_flush_task: Optional[asyncio.Task] = None
# Set when the flusher should exit (used by stop_flusher on shutdown).
_flush_stop_event: Optional[asyncio.Event] = None

# Counters for observability + tests.
_stats = {
    "hits": 0,
    "misses": 0,
    "neg_hits": 0,
    "invalidations": 0,
    "flushes": 0,
    "flush_rows": 0,
}


def _snapshot_from_asset(asset: Any) -> PromptAssetSnapshot:
    """Convert a ``PromptAsset`` ORM instance to a detached snapshot.

    Defensive about ``None`` numeric/text fields so callers never have to
    re-check; mirrors the defaults in ``PromptRegistry.resolve_route``.
    """
    return PromptAssetSnapshot(
        id=asset.id,
        task_type=asset.task_type,
        name=asset.name or "",
        mode=asset.mode or "text",
        system_prompt=asset.system_prompt or "",
        endpoint_id=asset.endpoint_id,
        model_name=asset.model_name or "",
        temperature=asset.temperature if asset.temperature is not None else 0.7,
        max_tokens=asset.max_tokens if asset.max_tokens is not None else 8192,
        model_tier=getattr(asset, "model_tier", None),
        output_schema=asset.output_schema,
        user_template=asset.user_template or "",
    )


async def get_snapshot(task_type: str, db: Any) -> Optional[PromptAssetSnapshot]:
    """Read-through cache for the active PromptAsset of ``task_type``.

    Returns ``None`` when DB has no active row (cached briefly so we don't
    hammer the DB for unknown task_types). On any cache/DB error we fall back
    to a direct SELECT and never raise out of the cache layer.
    """
    now = time.time()
    cached = _snap_cache.get(task_type)
    if cached is not None and cached[1] > now:
        if cached[0] is None:
            _stats["neg_hits"] += 1
        else:
            _stats["hits"] += 1
        return cached[0]

    _stats["misses"] += 1
    # Note: import inside the function to avoid circulars at module import
    # time (prompt_registry imports prompt_cache via the runners).
    from sqlalchemy import select
    from app.models.prompt import PromptAsset

    try:
        result = await db.execute(
            select(PromptAsset)
            .where(
                PromptAsset.task_type == task_type,
                PromptAsset.is_active == 1,
            )
            .order_by(PromptAsset.version.desc())
            .limit(1)
        )
        asset = result.scalar_one_or_none()
    except Exception as exc:
        # Don't poison the cache on transient DB errors; just bypass.
        logger.debug("prompt_cache: DB read failed for %s: %s", task_type, exc)
        return None

    if asset is None:
        _snap_cache[task_type] = (None, now + NEG_TTL_SECONDS)
        return None

    snap = _snapshot_from_asset(asset)
    _snap_cache[task_type] = (snap, now + TTL_SECONDS)
    return snap


def invalidate(task_type: Optional[str] = None) -> None:
    """Drop one or all cache entries.

    Call after ``/prompts`` CRUD. ``task_type=None`` drops everything (use
    after bulk seed/import).
    """
    _stats["invalidations"] += 1
    if task_type is None:
        _snap_cache.clear()
    else:
        _snap_cache.pop(task_type, None)


def stats() -> dict[str, int]:
    """Snapshot of cache counters for observability + tests."""
    return dict(_stats)


def reset_for_tests() -> None:
    """Wipe cache + buffers + counters. Test-only."""
    _snap_cache.clear()
    _pending_success.clear()
    _pending_fail.clear()
    for k in _stats:
        _stats[k] = 0


# ---------------------------------------------------------------------------
# Buffered counter writes
# ---------------------------------------------------------------------------


async def buffer_track_result(asset_id: Any, success: bool) -> None:
    """Record a success/fail tick in memory; flushed in the background.

    Replaces ``PromptRegistry.track_result``. The hot path no longer touches
    the request session, so the C2-class deadlock (UPDATE prompt_assets
    blocked behind the outer request tx's row lock) cannot recur.
    """
    if asset_id is None:
        return
    key = str(asset_id)
    async with _pending_lock:
        if success:
            _pending_success[key] = _pending_success.get(key, 0) + 1
        else:
            _pending_fail[key] = _pending_fail.get(key, 0) + 1
    # Lazy-start the flusher on first usage so the cache works even when the
    # process didn't call start_flusher() (e.g. one-off scripts, celery).
    _ensure_flusher()


def pending_counts() -> dict[str, dict[str, int]]:
    """Inspect buffered counters (test/observability)."""
    return {
        "success": dict(_pending_success),
        "fail": dict(_pending_fail),
    }


async def flush_pending_counts() -> dict[str, int]:
    """Flush all buffered counters to DB in a single fresh-session tx.

    Returns ``{"success": <total ticks>, "fail": <total ticks>}`` for the
    flushed batch. If there is nothing to flush, returns zeros without
    opening a session.
    """
    async with _pending_lock:
        if not _pending_success and not _pending_fail:
            return {"success": 0, "fail": 0}
        success_snap = dict(_pending_success)
        fail_snap = dict(_pending_fail)
        _pending_success.clear()
        _pending_fail.clear()

    from sqlalchemy import update
    from app.db.session import async_session_factory
    from app.models.prompt import PromptAsset

    total_success = sum(success_snap.values())
    total_fail = sum(fail_snap.values())

    try:
        async with async_session_factory() as flush_db:
            async with flush_db.begin():
                for aid, n in success_snap.items():
                    if n <= 0:
                        continue
                    await flush_db.execute(
                        update(PromptAsset)
                        .where(PromptAsset.id == aid)
                        .values(success_count=PromptAsset.success_count + n)
                    )
                for aid, n in fail_snap.items():
                    if n <= 0:
                        continue
                    await flush_db.execute(
                        update(PromptAsset)
                        .where(PromptAsset.id == aid)
                        .values(fail_count=PromptAsset.fail_count + n)
                    )
    except Exception as exc:
        # On failure, restore the buffers so we don't lose ticks. The next
        # flush will retry. We don't propagate — this is best-effort
        # observability, not correctness-critical.
        logger.warning("prompt_cache: flush failed (%s); re-buffering", exc)
        async with _pending_lock:
            for aid, n in success_snap.items():
                _pending_success[aid] = _pending_success.get(aid, 0) + n
            for aid, n in fail_snap.items():
                _pending_fail[aid] = _pending_fail.get(aid, 0) + n
        return {"success": 0, "fail": 0}

    _stats["flushes"] += 1
    _stats["flush_rows"] += len(success_snap) + len(fail_snap)
    return {"success": total_success, "fail": total_fail}


# ---------------------------------------------------------------------------
# Background flusher lifecycle
# ---------------------------------------------------------------------------


def _ensure_flusher() -> None:
    """Start the background flusher if not already running."""
    global _flush_task, _flush_stop_event
    if _flush_task is not None and not _flush_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. called from sync test setup). Skip; the next
        # async caller will retry.
        return
    _flush_stop_event = asyncio.Event()
    _flush_task = loop.create_task(_flusher_loop(_flush_stop_event), name="prompt_cache.flusher")


async def _flusher_loop(stop_event: asyncio.Event) -> None:
    """Periodically flush pending counters until stop_event is set."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=FLUSH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        try:
            await flush_pending_counts()
        except Exception:
            logger.exception("prompt_cache: unexpected flusher error")


def start_flusher() -> None:
    """Lifespan hook: ensure the flusher task is running."""
    _ensure_flusher()


async def stop_flusher() -> None:
    """Lifespan hook: signal the flusher to exit and drain pending counts."""
    global _flush_task, _flush_stop_event
    if _flush_stop_event is not None:
        _flush_stop_event.set()
    if _flush_task is not None:
        try:
            await asyncio.wait_for(_flush_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _flush_task.cancel()
    _flush_task = None
    _flush_stop_event = None
    # Final drain so we don't lose ticks on shutdown.
    try:
        await flush_pending_counts()
    except Exception:
        logger.exception("prompt_cache: shutdown flush failed")
