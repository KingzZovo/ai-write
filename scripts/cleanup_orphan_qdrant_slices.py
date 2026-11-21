#!/usr/bin/env python3
"""v1.7 X3: cleanup orphan slice points in Qdrant.

Background:
  Early versions of ingestion used random uuid4() Qdrant point IDs.
  Re-ingest of the same book produced a fresh set of points each time.
  Current code (qdrant_store.py) uses deterministic IDs derived from
  (book_id, slice_id) so a re-ingest now upserts in place. But the
  pre-deterministic-ID points remain — orphan rows whose slice_id no
  longer exists in PG (or duplicate copies of slices that do exist,
  written under random IDs).

  This script reconciles a Qdrant collection with PG
  ``reference_book_slices`` and removes orphan points.

Reconciliation:
  - PG truth: SELECT id FROM reference_book_slices.
  - Qdrant: scroll the target collection, read payload['slice_id'].
  - Orphan = Qdrant point whose slice_id is NOT in the PG truth set.
  - Action: delete orphans by point_id in batches.

Usage:
  # Dry-run (default): only report
  python scripts/cleanup_orphan_qdrant_slices.py --collection style_samples_redacted

  # Apply
  python scripts/cleanup_orphan_qdrant_slices.py --collection style_samples_redacted --apply

  # Multiple collections
  python scripts/cleanup_orphan_qdrant_slices.py \
      --collection style_samples_redacted \
      --collection beat_sheets \
      --collection style_profiles \
      --apply

Safety:
  - Default mode is --dry-run (no deletes).
  - Always reports counts before deleting.
  - PG is treated as source-of-truth: any (book_id, slice_id) NOT in PG
    is considered orphan.
  - This is idempotent: running it twice in a row is a no-op on the
    second run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Iterable

logger = logging.getLogger("cleanup_orphan_qdrant_slices")


async def fetch_pg_slice_ids() -> set[str]:
    """Return the set of slice IDs (UUID strings) present in PG."""
    from sqlalchemy import text

    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        rows = (await db.execute(text("SELECT id::text FROM reference_book_slices"))).all()
    return {r[0] for r in rows}


async def scroll_collection(qc, collection: str) -> list[tuple[int | str, str | None]]:
    """Scroll an entire Qdrant collection. Returns [(point_id, slice_id), ...].

    slice_id is None when payload is missing it (treat as orphan).
    """
    out: list[tuple[int | str, str | None]] = []
    offset = None
    page_size = 512
    while True:
        result, next_offset = await qc.scroll(
            collection_name=collection,
            with_payload=True,
            with_vectors=False,
            limit=page_size,
            offset=offset,
        )
        for p in result:
            payload = p.payload or {}
            out.append((p.id, payload.get("slice_id")))
        if next_offset is None:
            break
        offset = next_offset
    return out


def compute_orphan_ids(
    qdrant_points: Iterable[tuple[int | str, str | None]],
    pg_slice_ids: set[str],
) -> tuple[list[int | str], int, int]:
    """Pure logic: return (orphan_point_ids, orphan_count, kept_count).

    Extracted so it is unit-testable without Qdrant or PG.
    """
    orphan_ids: list[int | str] = []
    kept = 0
    for pid, sid in qdrant_points:
        if sid is None or sid not in pg_slice_ids:
            orphan_ids.append(pid)
        else:
            kept += 1
    return orphan_ids, len(orphan_ids), kept


async def delete_in_batches(qc, collection: str, point_ids: list, batch_size: int = 200) -> int:
    """Delete point ids in batches via Qdrant client. Returns count deleted."""
    from qdrant_client.models import PointIdsList

    deleted = 0
    for i in range(0, len(point_ids), batch_size):
        chunk = point_ids[i : i + batch_size]
        await qc.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=chunk),
        )
        deleted += len(chunk)
        logger.info("  deleted %d/%d", deleted, len(point_ids))
    return deleted


async def reconcile_collection(qc, collection: str, pg_slice_ids: set[str], apply: bool) -> dict:
    points = await scroll_collection(qc, collection)
    orphan_ids, orphan_count, kept = compute_orphan_ids(points, pg_slice_ids)
    qdrant_total = len(points)
    logger.info(
        "[%s] qdrant_total=%d pg_truth=%d orphan=%d kept=%d",
        collection,
        qdrant_total,
        len(pg_slice_ids),
        orphan_count,
        kept,
    )
    deleted = 0
    if apply and orphan_ids:
        deleted = await delete_in_batches(qc, collection, orphan_ids)
        logger.info("[%s] applied: deleted %d orphan points", collection, deleted)
    elif orphan_ids:
        logger.info("[%s] DRY-RUN: would delete %d orphans (use --apply)", collection, orphan_count)
    return {
        "collection": collection,
        "qdrant_total": qdrant_total,
        "pg_truth": len(pg_slice_ids),
        "orphan": orphan_count,
        "kept": kept,
        "deleted": deleted,
    }


async def main_async(collections: list[str], apply: bool) -> int:
    from qdrant_client import AsyncQdrantClient

    from app.config import settings

    pg_slice_ids = await fetch_pg_slice_ids()
    logger.info("PG truth: %d reference_book_slices rows", len(pg_slice_ids))

    qc = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    try:
        results = []
        for col in collections:
            r = await reconcile_collection(qc, col, pg_slice_ids, apply)
            results.append(r)
        logger.info("\n=== SUMMARY ===")
        for r in results:
            mode = "applied" if apply else "dry-run"
            logger.info(
                "  %s [%s]: qdrant=%d pg=%d orphan=%d kept=%d deleted=%d",
                r["collection"], mode, r["qdrant_total"], r["pg_truth"],
                r["orphan"], r["kept"], r["deleted"],
            )
        return 0
    finally:
        await qc.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconcile Qdrant slice collections with PG.")
    p.add_argument(
        "--collection",
        action="append",
        required=True,
        help="Qdrant collection to reconcile (repeatable)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete orphans (default is dry-run).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return asyncio.run(main_async(args.collection, args.apply))


if __name__ == "__main__":
    sys.exit(main())
