"""Durable whole-book commands, worker ownership and chapter result identity."""
from alembic import op

revision = "a1001921"
down_revision = "a1001920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier installations created these two tables through Base.create_all.
    # A clean migration-only installation must receive them as well.
    op.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            volume_id uuid, state varchar(20), current_chapter_idx integer,
            total_chapters integer, completed_chapters integer, review_round integer,
            max_review_rounds integer, snapshot_json json, error_message text,
            last_error_chapter integer, started_at timestamptz, completed_at timestamptz,
            created_at timestamptz, updated_at timestamptz
        );
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_chapter_statuses (
            id uuid PRIMARY KEY,
            pipeline_id uuid NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
            chapter_id uuid NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
            chapter_idx integer NOT NULL, state varchar(20), review_round integer,
            word_count integer, quality_score double precision, error_message text,
            started_at timestamptz, completed_at timestamptz
        );
    """)
    for name, kind in {
        "options_json": "json", "command_id": "uuid", "worker_id": "uuid",
        "queued_at": "timestamptz", "dispatched_at": "timestamptz", "heartbeat_at": "timestamptz",
    }.items():
        op.execute(f"ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS {name} {kind}")
    for name, kind in {
        "generation_task_id": "uuid", "attempt_count": "integer NOT NULL DEFAULT 0", "saved_hash": "varchar(64)",
        "saved_updated_at": "timestamptz",
    }.items():
        op.execute(f"ALTER TABLE pipeline_chapter_statuses ADD COLUMN IF NOT EXISTS {name} {kind}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_runs_dispatch ON pipeline_runs (state, dispatched_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_chapter_statuses_run ON pipeline_chapter_statuses (pipeline_id)")


def downgrade() -> None:
    # Do not drop the historical tables or their chapters/snapshots.
    op.execute("DROP INDEX IF EXISTS ix_pipeline_runs_dispatch")
    op.execute("DROP INDEX IF EXISTS ix_pipeline_chapter_statuses_run")
    for name in ("options_json", "command_id", "worker_id", "queued_at", "dispatched_at", "heartbeat_at"):
        op.execute(f"ALTER TABLE pipeline_runs DROP COLUMN IF EXISTS {name}")
    for name in ("generation_task_id", "attempt_count", "saved_hash", "saved_updated_at"):
        op.execute(f"ALTER TABLE pipeline_chapter_statuses DROP COLUMN IF EXISTS {name}")
