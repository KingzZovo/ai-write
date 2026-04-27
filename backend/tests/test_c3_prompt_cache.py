"""v1.5.0 C3 — prompt_cache regression tests.

Verifies the three contracts that earn C3 its keep:

1. Read-through TTL cache: a hot ``get_snapshot`` does NOT issue a second
   SELECT for the same task_type until TTL expires or invalidate() is called.
2. Negative cache: missing task_types are remembered briefly so we don't
   hammer the DB on typos / un-seeded prompts.
3. Buffered counter writes: ``buffer_track_result`` increments are held in
   memory and only flushed by ``flush_pending_counts`` — the request
   session is NEVER touched, eliminating the C2 deadlock class by
   construction. The flush MUST go through ``async_session_factory``.

These are pure unit tests over the cache module (no real DB / LLM / celery).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.services import prompt_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset(
    *,
    task_type: str = "generation",
    asset_id: UUID | None = None,
    endpoint_id: UUID | None = None,
    model_tier: str | None = "flagship",
    system_prompt: str = "You are a writer.",
) -> SimpleNamespace:
    """Build a duck-typed PromptAsset stand-in."""
    return SimpleNamespace(
        id=asset_id or uuid4(),
        task_type=task_type,
        name="test",
        mode="text",
        system_prompt=system_prompt,
        endpoint_id=endpoint_id or uuid4(),
        model_name="gpt-test",
        temperature=0.7,
        max_tokens=8192,
        model_tier=model_tier,
        output_schema=None,
        user_template="",
    )


def _fake_db_returning(asset: SimpleNamespace | None) -> AsyncMock:
    """Build an async DB stand-in whose ``execute`` returns one ``asset``."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=asset)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture(autouse=True)
def _reset_cache():
    """Wipe cache state between tests so order doesn't matter."""
    prompt_cache.reset_for_tests()
    yield
    prompt_cache.reset_for_tests()


# ---------------------------------------------------------------------------
# 1. Snapshot cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_snapshot_caches_after_first_hit() -> None:
    asset = _make_asset()
    db = _fake_db_returning(asset)

    snap1 = await prompt_cache.get_snapshot("generation", db)
    snap2 = await prompt_cache.get_snapshot("generation", db)
    snap3 = await prompt_cache.get_snapshot("generation", db)

    assert snap1 is not None and snap1.id == asset.id
    # Same id on every call.
    assert snap2.id == asset.id and snap3.id == asset.id
    # DB SELECT issued exactly once for three reads.
    assert db.execute.await_count == 1
    stats = prompt_cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 2


@pytest.mark.asyncio
async def test_get_snapshot_returns_detached_dataclass() -> None:
    """Snapshots survive across sessions — they are NOT ORM instances."""
    asset = _make_asset()
    db = _fake_db_returning(asset)

    snap = await prompt_cache.get_snapshot("generation", db)
    assert isinstance(snap, prompt_cache.PromptAssetSnapshot)
    # Frozen — attempting to mutate raises.
    with pytest.raises(Exception):
        snap.system_prompt = "hacked"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_invalidate_drops_cached_entry() -> None:
    asset = _make_asset()
    db = _fake_db_returning(asset)

    await prompt_cache.get_snapshot("generation", db)
    await prompt_cache.get_snapshot("generation", db)
    assert db.execute.await_count == 1

    prompt_cache.invalidate("generation")
    await prompt_cache.get_snapshot("generation", db)
    # After invalidate, the next read must go to DB.
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_all_drops_every_entry() -> None:
    a1 = _make_asset(task_type="generation")
    a2 = _make_asset(task_type="evaluation")

    # Two different mock dbs so the per-task SELECT counts are independent.
    db1 = _fake_db_returning(a1)
    db2 = _fake_db_returning(a2)
    await prompt_cache.get_snapshot("generation", db1)
    await prompt_cache.get_snapshot("evaluation", db2)

    prompt_cache.invalidate()  # global drop

    await prompt_cache.get_snapshot("generation", db1)
    await prompt_cache.get_snapshot("evaluation", db2)
    assert db1.execute.await_count == 2
    assert db2.execute.await_count == 2


@pytest.mark.asyncio
async def test_negative_cache_remembers_missing_task() -> None:
    db = _fake_db_returning(None)

    snap1 = await prompt_cache.get_snapshot("nonexistent", db)
    snap2 = await prompt_cache.get_snapshot("nonexistent", db)

    assert snap1 is None and snap2 is None
    # Negative cache prevents the second SELECT.
    assert db.execute.await_count == 1
    assert prompt_cache.stats()["neg_hits"] == 1


@pytest.mark.asyncio
async def test_get_snapshot_swallows_db_errors() -> None:
    """Cache must not propagate transient DB errors — just bypass."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("connection lost"))

    snap = await prompt_cache.get_snapshot("generation", db)
    assert snap is None
    # And we did NOT poison the cache with a negative entry on transient error.
    snap2 = await prompt_cache.get_snapshot("generation", db)
    assert db.execute.await_count == 2
    assert snap2 is None


# ---------------------------------------------------------------------------
# 2. Buffered counter writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_track_result_does_not_touch_db() -> None:
    """Hot path must NEVER hit the DB — buffers in memory only."""
    aid = uuid4()

    with patch(
        "app.db.session.async_session_factory"
    ) as factory_mock:
        await prompt_cache.buffer_track_result(aid, success=True)
        await prompt_cache.buffer_track_result(aid, success=True)
        await prompt_cache.buffer_track_result(aid, success=False)
        # Critical: the factory must NOT have been called yet.
        factory_mock.assert_not_called()

    pending = prompt_cache.pending_counts()
    assert pending["success"][str(aid)] == 2
    assert pending["fail"][str(aid)] == 1


@pytest.mark.asyncio
async def test_flush_pending_counts_uses_fresh_session() -> None:
    """Flush MUST go through async_session_factory, never the request db.

    This is the C2-deadlock prevention contract: the per-call UPDATE is
    moved off the request session entirely.
    """
    aid_ok = uuid4()
    aid_fail = uuid4()
    await prompt_cache.buffer_track_result(aid_ok, success=True)
    await prompt_cache.buffer_track_result(aid_ok, success=True)
    await prompt_cache.buffer_track_result(aid_fail, success=False)

    # Build a mock session that accepts ``async with factory() as db`` and
    # ``async with db.begin():`` then records db.execute calls.
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    txn = AsyncMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    fake_db.begin = MagicMock(return_value=txn)

    factory = MagicMock(return_value=fake_db)

    with patch("app.db.session.async_session_factory", factory):
        result = await prompt_cache.flush_pending_counts()

    assert result == {"success": 2, "fail": 1}
    factory.assert_called_once()  # fresh session opened
    # 1 success row + 1 fail row = 2 UPDATE statements.
    assert fake_db.execute.await_count == 2
    # Buffers drained.
    assert prompt_cache.pending_counts() == {"success": {}, "fail": {}}


@pytest.mark.asyncio
async def test_flush_with_empty_buffers_skips_session_open() -> None:
    factory = MagicMock()
    with patch("app.db.session.async_session_factory", factory):
        result = await prompt_cache.flush_pending_counts()
    assert result == {"success": 0, "fail": 0}
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_flush_failure_re_buffers_for_retry() -> None:
    """On flush error, ticks must be returned to the buffer (no data loss)."""
    aid = uuid4()
    await prompt_cache.buffer_track_result(aid, success=True)
    await prompt_cache.buffer_track_result(aid, success=True)

    # async_session_factory raises — e.g. DB temporarily down.
    factory = MagicMock(side_effect=RuntimeError("db down"))
    with patch("app.db.session.async_session_factory", factory):
        result = await prompt_cache.flush_pending_counts()

    assert result == {"success": 0, "fail": 0}
    # Buffer restored — the next flush will retry.
    assert prompt_cache.pending_counts()["success"][str(aid)] == 2


@pytest.mark.asyncio
async def test_buffer_with_none_asset_id_is_noop() -> None:
    """Defensive: prompts without prompt_id should not trip the cache."""
    await prompt_cache.buffer_track_result(None, success=True)
    assert prompt_cache.pending_counts() == {"success": {}, "fail": {}}


# ---------------------------------------------------------------------------
# 3. Resolver integration (cache + fallback + RouteSpec build)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_uses_cache_and_returns_route_plus_tier() -> None:
    """_resolve_route_and_tier_cached collapses both DB hits into one."""
    from app.services.prompt_registry import _resolve_route_and_tier_cached

    asset = _make_asset(task_type="generation", model_tier="flagship")
    db = _fake_db_returning(asset)

    route, tier = await _resolve_route_and_tier_cached("generation", db)
    assert route.prompt_id == asset.id
    assert route.endpoint_id == asset.endpoint_id
    assert route.system_prompt == asset.system_prompt
    assert tier == "flagship"
    # One SELECT for the resolver, despite needing both route and tier.
    assert db.execute.await_count == 1

    # Second resolver call within TTL: zero new SELECTs.
    route2, tier2 = await _resolve_route_and_tier_cached("generation", db)
    assert route2.prompt_id == asset.id and tier2 == "flagship"
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolver_raises_when_no_prompt_registered() -> None:
    from app.services.prompt_registry import _resolve_route_and_tier_cached

    db = _fake_db_returning(None)
    with pytest.raises(ValueError, match="No active prompt registered"):
        await _resolve_route_and_tier_cached("never_seen_task", db)


@pytest.mark.asyncio
async def test_resolver_raises_when_endpoint_unset() -> None:
    from app.services.prompt_registry import _resolve_route_and_tier_cached

    asset = _make_asset(endpoint_id=None)
    asset.endpoint_id = None  # explicit
    db = _fake_db_returning(asset)
    with pytest.raises(ValueError, match="has no endpoint configured"):
        await _resolve_route_and_tier_cached("generation", db)
