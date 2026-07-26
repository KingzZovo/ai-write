"""llm_call_logs: convert to native monthly RANGE partitioning by created_at.

Why this table (measured 2026-07-26, live DB):

    llm_call_logs         1274 MB total (168 MB heap, rest TOAST+indexes),
                          211,227 rows, 2026-04-24 .. today, growing with
                          every LLM call (a 300万字 run adds ~40-60k rows of
                          large JSON payloads). Clear unbounded grower.
    reference_book_slices 258 MB, 27k rows, static since May (decompile
                          artifacts, bounded per book) — not partitioned.
    chapters              18 MB, 872 rows — NOT partitioned, see a1001917.

Strategy: rename-and-attach (no data copy).

    1. created_at SET NOT NULL (required for the partition key).
    2. Add CHECK (created_at < <upper>) to the existing table, where
       <upper> = first day of the month AFTER the month this migration
       runs in (computed from now() at runtime).
    3. Rename llm_call_logs -> llm_call_logs_hist (+ rename its indexes
       out of the way; index names are schema-global). Drop its old (id)
       PK — a partition cannot have a second PRIMARY KEY beside the
       parent's; the parent PK index built at attach covers id lookups.
    4. Create the partitioned parent `llm_call_logs` with the identical
       column list / defaults / CHECK / FKs, PK (id, created_at)
       (partitioned unique constraints must include the partition key),
       and the canonical index names.
    5. ATTACH llm_call_logs_hist FOR VALUES FROM (MINVALUE) TO (<upper>).
       The pre-added CHECK + NOT NULL let Postgres skip the validation
       scan at attach time.
    6. Create monthly partitions for <upper>'s month + the next 3, plus
       a DEFAULT partition as insert-failure insurance if partition
       pre-creation (tasks.maintain_llm_log_partitions) ever stalls.

Lock / duration expectations (measured table: 168 MB heap, 211k rows):

    * Steps 1-2 each scan the heap once under ACCESS EXCLUSIVE (~1-3 s
      each on this table).
    * Steps 3-6 run in the same transaction; ATTACH builds the two
      indexes the old table lacks — the (id, created_at) PK index and
      ix_llm_call_logs_created_at — ~2-5 s total.
    * Writers (llm_call_logger inserts) block for the duration and then
      proceed; expect a single < ~15 s stall. lock_timeout is set to 10 s
      so the migration aborts cleanly instead of queueing behind a
      long-running query (just re-run it).
    * Do NOT run within a few seconds of a month boundary: a row stamped
      in the new month before the rename would fail the CHECK in step 2
      (the migration aborts safely; re-run).

Runbook (orchestrator):

    docker exec ai-write-postgres-1 pg_dump -U postgres -Fc aiwrite > backup.dump
    cd backend && alembic upgrade head

ORM note: app/models/call_log.py keeps a single-column mapper PK (`id`)
so `db.get(LLMCallLog, id)` keeps working; the composite DB PK
(id, created_at) is documented there. `id` stays a uuid4, so it remains
unique in practice across partitions.

Downgrade copies all rows back into a plain table (rewrites ~1.3 GB) —
functional but heavy; treat as emergency-only.
"""

from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "a1001916"
down_revision = "a1001915"
branch_labels = None
depends_on = None


# Column list shared by the partitioned parent (upgrade) and the flat
# rebuild (downgrade). Mirrors the live schema exactly.
_COLUMNS_SQL = """
    id uuid NOT NULL,
    prompt_id uuid,
    task_type varchar(50) NOT NULL,
    project_id uuid,
    chapter_id uuid,
    messages_json json NOT NULL,
    rag_hits_json json,
    response_text text DEFAULT ''::text,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    latency_ms integer DEFAULT 0,
    model varchar(200) DEFAULT ''::character varying,
    endpoint_id uuid,
    status varchar(20) DEFAULT 'ok'::character varying,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    tier_used varchar(20),
    fallback_reason varchar(200),
    attempt_index integer NOT NULL DEFAULT 0
"""

# Verbatim copy of the live CHECK so ATTACH merges (same name + same
# expression) instead of erroring.
_TIER_CHECK_SQL = """
    CONSTRAINT ck_llm_call_logs_tier_used CHECK (
        tier_used IS NULL OR (tier_used::text = ANY (
            ARRAY['flagship'::character varying,
                  'standard'::character varying,
                  'small'::character varying,
                  'distill'::character varying,
                  'embedding'::character varying]::text[]))
    )
"""

_FKS_SQL = """
    CONSTRAINT llm_call_logs_prompt_id_fkey FOREIGN KEY (prompt_id)
        REFERENCES prompt_assets(id) ON DELETE SET NULL,
    CONSTRAINT llm_call_logs_project_id_fkey FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE SET NULL,
    CONSTRAINT llm_call_logs_chapter_id_fkey FOREIGN KEY (chapter_id)
        REFERENCES chapters(id) ON DELETE SET NULL,
    CONSTRAINT llm_call_logs_endpoint_id_fkey FOREIGN KEY (endpoint_id)
        REFERENCES llm_endpoints(id) ON DELETE SET NULL
"""


def _add_months(d: date, n: int) -> date:
    """First day of the month `n` months after `d`'s month."""
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _ts(d: date) -> str:
    return f"{d.isoformat()} 00:00:00+00"


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast instead of queueing an ACCESS EXCLUSIVE request behind a
    # long-running query (which would block all other traffic too).
    op.execute("SET LOCAL lock_timeout = '10s'")

    # Upper bound of the historical partition: first day of NEXT month,
    # relative to the database clock at migration time.
    upper: date = bind.execute(
        sa.text(
            "SELECT (date_trunc('month', now() AT TIME ZONE 'UTC')"
            " + interval '1 month')::date"
        )
    ).scalar()

    # 1) Partition key must be NOT NULL (full scan; live rows verified
    #    to contain no NULLs — default has always been now()).
    op.execute("ALTER TABLE llm_call_logs ALTER COLUMN created_at SET NOT NULL")

    # 2) Range check so ATTACH below can skip its validation scan.
    op.execute(
        f"ALTER TABLE llm_call_logs ADD CONSTRAINT ck_llm_call_logs_hist_upper "
        f"CHECK (created_at < '{_ts(upper)}')"
    )

    # 3) Move the old table (and its schema-global index names) aside.
    op.execute("ALTER TABLE llm_call_logs RENAME TO llm_call_logs_hist")
    # A partition may not carry its own PRIMARY KEY next to the parent's
    # (rehearsed: ATTACH fails with "multiple primary keys ... not
    # allowed"). Drop the old (id) PK; ATTACH below builds the parent's
    # (id, created_at) unique index on this partition, which still serves
    # id lookups via its leading column.
    op.execute("ALTER TABLE llm_call_logs_hist DROP CONSTRAINT llm_call_logs_pkey")
    op.execute(
        "ALTER INDEX ix_llm_call_logs_chapter_created"
        " RENAME TO ix_llm_call_logs_hist_chapter_created"
    )
    op.execute(
        "ALTER INDEX ix_llm_call_logs_project_created"
        " RENAME TO ix_llm_call_logs_hist_project_created"
    )
    op.execute(
        "ALTER INDEX ix_llm_call_logs_task_type"
        " RENAME TO ix_llm_call_logs_hist_task_type"
    )
    op.execute(
        "ALTER INDEX ix_llm_call_logs_tier_used"
        " RENAME TO ix_llm_call_logs_hist_tier_used"
    )

    # 4) Partitioned parent with the canonical name and index names.
    op.execute(
        f"""
        CREATE TABLE llm_call_logs (
            {_COLUMNS_SQL},
            CONSTRAINT llm_call_logs_pkey PRIMARY KEY (id, created_at),
            {_TIER_CHECK_SQL},
            {_FKS_SQL}
        ) PARTITION BY RANGE (created_at)
        """
    )
    op.execute(
        "CREATE INDEX ix_llm_call_logs_chapter_created"
        " ON llm_call_logs (chapter_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_llm_call_logs_project_created"
        " ON llm_call_logs (project_id, created_at)"
    )
    op.execute("CREATE INDEX ix_llm_call_logs_task_type ON llm_call_logs (task_type)")
    op.execute("CREATE INDEX ix_llm_call_logs_tier_used ON llm_call_logs (tier_used)")
    # Declared in the ORM (created_at index=True) but never created on the
    # old table; serves /api/call-logs unfiltered ORDER BY created_at DESC.
    op.execute("CREATE INDEX ix_llm_call_logs_created_at ON llm_call_logs (created_at)")

    # 5) Attach all existing rows as the historical partition. Existing
    #    compatible indexes are matched by definition (names irrelevant);
    #    only the composite PK index and created_at index get built here.
    op.execute(
        f"ALTER TABLE llm_call_logs ATTACH PARTITION llm_call_logs_hist"
        f" FOR VALUES FROM (MINVALUE) TO ('{_ts(upper)}')"
    )

    # 6) Monthly partitions: upper's month + 3 ahead, plus a DEFAULT
    #    catch-all. tasks.maintain_llm_log_partitions keeps rolling these
    #    forward and applies retention.
    for i in range(4):
        lo = _add_months(upper, i)
        hi = _add_months(upper, i + 1)
        op.execute(
            f"CREATE TABLE IF NOT EXISTS llm_call_logs_y{lo.year:04d}m{lo.month:02d}"
            f" PARTITION OF llm_call_logs"
            f" FOR VALUES FROM ('{_ts(lo)}') TO ('{_ts(hi)}')"
        )
    op.execute(
        "CREATE TABLE IF NOT EXISTS llm_call_logs_default"
        " PARTITION OF llm_call_logs DEFAULT"
    )


def downgrade() -> None:
    # Rebuild a flat table by copying every row out of the partitions.
    # Rewrites the full ~1.3 GB — emergency use only.
    op.execute(
        f"""
        CREATE TABLE llm_call_logs_flat (
            {_COLUMNS_SQL},
            CONSTRAINT llm_call_logs_flat_pk PRIMARY KEY (id),
            {_TIER_CHECK_SQL},
            {_FKS_SQL}
        )
        """
    )
    op.execute("INSERT INTO llm_call_logs_flat SELECT * FROM llm_call_logs")
    op.execute("DROP TABLE llm_call_logs CASCADE")  # drops all partitions
    op.execute("ALTER TABLE llm_call_logs_flat RENAME TO llm_call_logs")
    # Renames the constraint AND its underlying index in one step.
    op.execute(
        "ALTER TABLE llm_call_logs RENAME CONSTRAINT llm_call_logs_flat_pk"
        " TO llm_call_logs_pkey"
    )
    op.execute(
        "CREATE INDEX ix_llm_call_logs_chapter_created"
        " ON llm_call_logs (chapter_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_llm_call_logs_project_created"
        " ON llm_call_logs (project_id, created_at)"
    )
    op.execute("CREATE INDEX ix_llm_call_logs_task_type ON llm_call_logs (task_type)")
    op.execute("CREATE INDEX ix_llm_call_logs_tier_used ON llm_call_logs (tier_used)")
    op.execute("ALTER TABLE llm_call_logs ALTER COLUMN created_at DROP NOT NULL")
