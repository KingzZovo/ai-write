"""v1.9.2 C2/F1: style_stats table (whole-book style statistics)

One row per project holding deterministic book-level style statistics
(tic frequencies, recent n-gram tics, cross-chapter repeated sentences,
ending/opening shape). Recomputed in the background after chapter finalize;
fed into generation (dampen-your-tics mirror) and evaluation (raw numbers).

Adapted from voocel/ainovel-cli stylestat (design idea; wording our own).
"""

from alembic import op

revision = "a1001911"
down_revision = "a1001910"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS style_stats (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          stats_json jsonb,
          chapter_count integer,
          computed_at timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_style_stats_project_id
          ON style_stats (project_id);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_style_stats_project'
          ) THEN
            ALTER TABLE style_stats
              ADD CONSTRAINT uq_style_stats_project UNIQUE (project_id);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS style_stats
          DROP CONSTRAINT IF EXISTS uq_style_stats_project;
        DROP INDEX IF EXISTS ix_style_stats_project_id;
        DROP TABLE IF EXISTS style_stats;
        """
    )
