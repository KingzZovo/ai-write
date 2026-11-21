"""Partition maintenance + retention tasks (v1.11 scaling groundwork).

Companion to migration a1001916 (llm_call_logs monthly RANGE partitions)
and a1001917 (chapter index wins). Two beat tasks:

    tasks.maintain_llm_log_partitions
        * pre-creates monthly partitions for the next
          LLM_LOG_PARTITION_PRECREATE_MONTHS months (default 3), so
          inserts never land in llm_call_logs_default;
        * drops partitions whose entire range is older than
          LLM_LOG_RETENTION_MONTHS (default 6; <= 0 disables dropping).
          DROP TABLE of a partition briefly takes ACCESS EXCLUSIVE on
          the parent — instantaneous, no row scans.
        * no-ops gracefully (with a "skipped" result) until the
          partitioning migration has been applied.

    tasks.enforce_chapter_version_retention
        * keeps the newest CHAPTER_VERSION_KEEP_LAST versions per
          chapter. Default 0 = keep everything (authors may want full
          history); opt in explicitly via env. The active version is
          never deleted regardless of age.

All selection logic lives in pure functions (no DB) so it is unit-tested
without DDL: see tests/test_retention_partitions.py.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.tasks import celery_app

logger = logging.getLogger(__name__)

_PARENT = "llm_call_logs"
_HIST_PARTITION = "llm_call_logs_hist"
_DEFAULT_PARTITION = "llm_call_logs_default"

# `pg_get_expr(relpartbound, oid)` renders e.g.
#   FOR VALUES FROM (MINVALUE) TO ('2026-08-01 00:00:00+00')
_UPPER_BOUND_RE = re.compile(r"TO \('(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------------------
# Pure functions (unit-tested, no DB)
# ---------------------------------------------------------------------------

def add_months(d: date, n: int) -> date:
    """First day of the month `n` months after `d`'s month."""
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def partition_name(year: int, month: int) -> str:
    return f"{_PARENT}_y{year:04d}m{month:02d}"


def months_to_precreate(today: date, ahead: int) -> list[tuple[date, date]]:
    """(lower, upper) month bounds for current month .. current+ahead."""
    ahead = max(0, ahead)
    return [
        (add_months(today, i), add_months(today, i + 1)) for i in range(ahead + 1)
    ]


def parse_upper_bound(bound_expr: str | None) -> date | None:
    """Extract the exclusive upper bound date from a relpartbound expr.

    Returns None for the DEFAULT partition ("DEFAULT") or an unbounded /
    unparseable expression — such partitions are never retention-dropped.
    """
    if not bound_expr:
        return None
    m = _UPPER_BOUND_RE.search(bound_expr)
    if not m:
        return None
    return date.fromisoformat(m.group(1))


def retention_cutoff(today: date, retention_months: int) -> date:
    """First day of the oldest month that must be KEPT."""
    return add_months(today, -retention_months)


def select_partitions_to_drop(
    partitions: list[tuple[str, date | None]],
    today: date,
    retention_months: int,
) -> list[str]:
    """Names of partitions whose whole range is older than the cutoff.

    `partitions` is [(name, exclusive_upper_bound_or_None), ...]. A
    partition is droppable when upper <= cutoff (its newest possible row
    is still older than every month we keep). The DEFAULT partition and
    anything with an unparseable bound are never selected. The historical
    partition (llm_call_logs_hist) IS eligible once its whole range ages
    out — it is ordinary data, just attached in one piece.

    retention_months <= 0 disables dropping entirely.
    """
    if retention_months <= 0:
        return []
    cutoff = retention_cutoff(today, retention_months)
    return [
        name
        for name, upper in partitions
        if name != _DEFAULT_PARTITION and upper is not None and upper <= cutoff
    ]


def select_version_ids_to_delete(
    rows: list[tuple[UUID | str, UUID | str, datetime | None, int | None]],
    keep_last: int,
) -> list[UUID | str]:
    """Version ids to delete, keeping the newest `keep_last` per chapter.

    `rows` is [(id, chapter_id, created_at, is_active), ...] in any
    order. keep_last <= 0 means keep everything (the safe default). The
    active version (is_active truthy) is never deleted and does not
    consume a keep slot beyond its natural position.
    """
    if keep_last <= 0:
        return []
    by_chapter: dict[object, list[tuple]] = {}
    for row in rows:
        by_chapter.setdefault(row[1], []).append(row)
    doomed: list[UUID | str] = []

    def _sort_key(r: tuple) -> tuple:
        # timestamp() works for naive and aware datetimes alike; NULL
        # created_at sorts oldest. Tie-break on id for determinism.
        ts = r[2].timestamp() if r[2] is not None else float("-inf")
        return (ts, str(r[0]))

    for versions in by_chapter.values():
        versions.sort(key=_sort_key, reverse=True)
        for vid, _cid, _created, is_active in versions[keep_last:]:
            if not is_active:
                doomed.append(vid)
    return doomed


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

async def _maintain_llm_log_partitions_async() -> dict:
    from app.db.session import async_session_factory

    precreate = settings.LLM_LOG_PARTITION_PRECREATE_MONTHS
    retention = settings.LLM_LOG_RETENTION_MONTHS
    today = date.today()

    async with async_session_factory() as db:
        relkind = (
            await db.execute(
                text("SELECT relkind FROM pg_class WHERE relname = :n"),
                {"n": _PARENT},
            )
        ).scalar()
        if relkind != "p":
            # Migration a1001916 not applied yet (or plain table restored).
            return {"skipped": f"{_PARENT} is not partitioned (relkind={relkind!r})"}

        rows = (
            await db.execute(
                text(
                    """
                    SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
                    FROM pg_inherits i
                    JOIN pg_class c ON c.oid = i.inhrelid
                    WHERE i.inhparent = CAST(:parent AS regclass)
                    """
                ),
                {"parent": _PARENT},
            )
        ).all()
        existing = {name for name, _ in rows}
        bounds = [(name, parse_upper_bound(expr)) for name, expr in rows]

        created: list[str] = []
        for lo, hi in months_to_precreate(today, precreate):
            name = partition_name(lo.year, lo.month)
            if name in existing:
                continue
            await db.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} "
                    f"PARTITION OF {_PARENT} "
                    f"FOR VALUES FROM ('{lo.isoformat()} 00:00:00+00') "
                    f"TO ('{hi.isoformat()} 00:00:00+00')"
                )
            )
            created.append(name)

        dropped: list[str] = []
        for name in select_partitions_to_drop(bounds, today, retention):
            await db.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)

        await db.commit()

    if created or dropped:
        logger.info(
            "llm_call_logs partition maintenance: created=%s dropped=%s",
            created,
            dropped,
        )
    return {"created": created, "dropped": dropped, "retention_months": retention}


@celery_app.task(name="tasks.maintain_llm_log_partitions", bind=True, max_retries=1)
def maintain_llm_log_partitions(self) -> dict:
    # Lazy import: _run_async_safe is defined AFTER the task-module import
    # block in app/tasks/__init__.py (module-level import would be circular).
    from app.tasks import _run_async_safe

    return _run_async_safe(_maintain_llm_log_partitions_async())


async def _enforce_chapter_version_retention_async() -> dict:
    keep_last = settings.CHAPTER_VERSION_KEEP_LAST
    if keep_last <= 0:
        return {"skipped": "CHAPTER_VERSION_KEEP_LAST<=0 (keep all)", "deleted": 0}

    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT id, chapter_id, created_at, COALESCE(is_active, 0)
                    FROM chapter_versions
                    WHERE chapter_id IN (
                        SELECT chapter_id FROM chapter_versions
                        GROUP BY chapter_id HAVING count(*) > :k
                    )
                    """
                ),
                {"k": keep_last},
            )
        ).all()
        doomed = select_version_ids_to_delete([tuple(r) for r in rows], keep_last)
        if doomed:
            # parent_id self-FK is ON DELETE SET NULL — surviving children
            # of a deleted version keep their row, just lose the link.
            await db.execute(
                text(
                    "DELETE FROM chapter_versions"
                    " WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(v) for v in doomed]},
            )
        await db.commit()

    if doomed:
        logger.info("chapter_versions retention: deleted %d rows", len(doomed))
    return {"deleted": len(doomed), "keep_last": keep_last}


@celery_app.task(
    name="tasks.enforce_chapter_version_retention", bind=True, max_retries=1
)
def enforce_chapter_version_retention(self) -> dict:
    from app.tasks import _run_async_safe

    return _run_async_safe(_enforce_chapter_version_retention_async())
