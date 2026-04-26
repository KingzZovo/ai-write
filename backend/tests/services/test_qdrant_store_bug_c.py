"""Bug C regression tests for qdrant_store.py.

Before the Bug C fix, the three v0.6 decompile store helpers
(store_style_profile / store_beat_sheet / store_style_sample_redacted)
swallowed any exception from ``client.upsert`` and silently returned a
fabricated ``point_id``. Their callers in reference_ingestor.py would
then happily commit a StyleProfileCard / BeatSheetCard row to Postgres
pointing at a Qdrant point that does not exist, producing PG↔Qdrant
divergence (orphan PG cards).

The fix re-raises after logging. These tests pin that behaviour so the
regression cannot silently come back.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.qdrant_store import QdrantStore


class _BoomError(RuntimeError):
    """Sentinel error used to assert the exact exception is re-raised."""


def _store_with_failing_upsert() -> tuple[QdrantStore, MagicMock]:
    client = MagicMock()
    client.upsert = AsyncMock(side_effect=_BoomError("simulated qdrant outage"))
    return QdrantStore(client=client), client


@pytest.mark.asyncio
async def test_store_style_profile_raises_on_upsert_failure() -> None:
    store, client = _store_with_failing_upsert()
    with pytest.raises(_BoomError, match="simulated qdrant outage"):
        await store.store_style_profile(
            "book-1", "slice-1", {"pov": "first"}, [0.1] * 16
        )
    client.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_beat_sheet_raises_on_upsert_failure() -> None:
    store, client = _store_with_failing_upsert()
    with pytest.raises(_BoomError, match="simulated qdrant outage"):
        await store.store_beat_sheet(
            "book-1", "slice-1", {"scene_type": "action"}, [0.1] * 16
        )
    client.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_style_sample_redacted_raises_on_upsert_failure() -> None:
    store, client = _store_with_failing_upsert()
    with pytest.raises(_BoomError, match="simulated qdrant outage"):
        await store.store_style_sample_redacted(
            "book-1", "slice-1", "redacted text", [0.1] * 16
        )
    client.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_style_profile_returns_point_id_on_success() -> None:
    """Sanity: happy path still returns a stable deterministic point id."""
    client = MagicMock()
    client.upsert = AsyncMock(return_value=None)
    store = QdrantStore(client=client)
    point_id = await store.store_style_profile(
        "book-1", "slice-1", {"pov": "first"}, [0.1] * 16
    )
    assert isinstance(point_id, int)
    # Determinism: same (book_id, slice_id) → same id.
    point_id_2 = await store.store_style_profile(
        "book-1", "slice-1", {"pov": "first"}, [0.1] * 16
    )
    assert point_id == point_id_2
