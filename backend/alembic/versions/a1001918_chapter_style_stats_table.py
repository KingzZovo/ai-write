"""W14: chapter_style_stats table (incremental style/roster recompute).

One row per non-empty chapter: per-chapter style facts (stats_json) and
alias-folded character appearance counts (appearances_json), stamped with the
chapter's updated_at (source_updated_at) for staleness detection. The
whole-book style_stats row and the character_appearances roster aggregate
these rows, so the recompute task no longer reloads every chapter's
content_text after each accepted chapter (previously O(n^2) over a book's
lifetime) and roster counts become idempotent (absolute-value rebuild instead
of `appearance_count + c` increments).

No auto-backfill: rows are populated lazily by the recompute task (any
chapter without a row is treated as stale), or eagerly via
`recompute_style_stats(project_id, full=True)` for manual repair.
"""

from alembic import op

revision = "a1001918"
down_revision = "a1001917"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments (matches a1001912 style).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chapter_style_stats (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          chapter_id uuid NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
          global_idx integer NOT NULL DEFAULT 0,
          stats_json json,
          appearances_json json,
          source_updated_at timestamptz,
          computed_at timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chapter_style_stats_project_id
          ON chapter_style_stats (project_id);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_chapter_style_stats_chapter'
          ) THEN
            ALTER TABLE chapter_style_stats
              ADD CONSTRAINT uq_chapter_style_stats_chapter
              UNIQUE (chapter_id);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS chapter_style_stats
          DROP CONSTRAINT IF EXISTS uq_chapter_style_stats_chapter;
        DROP INDEX IF EXISTS ix_chapter_style_stats_project_id;
        DROP TABLE IF EXISTS chapter_style_stats;
        """
    )
