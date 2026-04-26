"""Backfill entity extraction for every existing chapter (B2', v1.5.0).

Iterates over all chapters with non-empty ``content_text`` and enqueues
``entities.extract_chapter`` for each. The task is idempotent (Neo4j
ExtractionMarker), so re-runs are safe.

Usage::

    docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 \\
        python -m app.scripts.backfill_entity_extraction --dry-run

    docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 \\
        python -m app.scripts.backfill_entity_extraction

    docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 \\
        python -m app.scripts.backfill_entity_extraction \\
            --project-id <uuid>

Flags:
  --dry-run         List rows that would be enqueued, do not dispatch.
  --project-id ID   Restrict to one project.
  --limit N         Cap the number of dispatches (debug aid).
  --countdown SEC   Spread dispatches across N seconds (rate limiting).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Sequence

logger = logging.getLogger("backfill_entity_extraction")


async def _collect_chapters(project_id: str | None, limit: int | None):
    from sqlalchemy import select
    from app.db.session import async_session_factory
    from app.models.project import Chapter, Volume

    async with async_session_factory() as db:
        stmt = (
            select(
                Volume.project_id,
                Chapter.id,
                Chapter.chapter_idx,
                Chapter.title,
            )
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Chapter.content_text.is_not(None))
            .where(Chapter.content_text != "")
            .order_by(Volume.project_id, Chapter.chapter_idx)
        )
        if project_id:
            stmt = stmt.where(Volume.project_id == str(project_id))
        if limit:
            stmt = stmt.limit(int(limit))
        result = await db.execute(stmt)
        return [
            {
                "project_id": str(row[0]),
                "chapter_id": str(row[1]),
                "chapter_idx": int(row[2]),
                "title": str(row[3] or ""),
            }
            for row in result.all()
        ]


def _dispatch_all(rows: Sequence[dict], countdown_total: int) -> int:
    from app.services.entity_dispatch import dispatch_entity_extraction

    if not rows:
        return 0
    enqueued = 0
    n = len(rows)
    for i, row in enumerate(rows):
        # Spread dispatches evenly across [0, countdown_total) seconds so
        # we do not hammer the LLM endpoint when running large backfills.
        countdown = int(countdown_total * i / n) if countdown_total > 0 else 0
        ok = dispatch_entity_extraction(
            project_id=row["project_id"],
            chapter_idx=row["chapter_idx"],
            chapter_id=row["chapter_id"],
            caller="scripts.backfill_entity_extraction",
            countdown=countdown,
        )
        if ok:
            enqueued += 1
    return enqueued


async def _amain(args: argparse.Namespace) -> int:
    rows = await _collect_chapters(args.project_id, args.limit)
    print(f"Discovered {len(rows)} chapter rows with content_text")
    if args.dry_run:
        for row in rows[:20]:
            print(
                f"  [dry-run] project={row['project_id']} "
                f"chapter_idx={row['chapter_idx']} id={row['chapter_id']} "
                f"title={row['title']!r}"
            )
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more (truncated for display)")
        return 0

    enqueued = _dispatch_all(rows, args.countdown)
    print(
        f"Backfill complete: enqueued {enqueued}/{len(rows)} "
        f"entity_extraction tasks (countdown spread = {args.countdown}s)"
    )
    return 0 if enqueued == len(rows) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--countdown", type=int, default=0,
                        help="Seconds to spread dispatches across (rate limit)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
