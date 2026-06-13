"""v1.9.2 C4/F3: narrative_compass table (direction anchor + completion gate)

One row per project: thematic ending direction, open-threads ledger, and an
estimated-scale *range* (min/max chapters & volumes). Updated when a new volume
is planned; consumed by generation (stay on the ending) and the
completion-readiness checklist.

Adapted from voocel/ainovel-cli compass (design idea; wording our own).
"""

from alembic import op

revision = "a1001913"
down_revision = "a1001912"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS narrative_compass (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          ending_direction text,
          open_threads jsonb,
          estimated_scale jsonb,
          last_updated timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_narrative_compass_project_id
          ON narrative_compass (project_id);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_narrative_compass_project'
          ) THEN
            ALTER TABLE narrative_compass
              ADD CONSTRAINT uq_narrative_compass_project UNIQUE (project_id);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS narrative_compass
          DROP CONSTRAINT IF EXISTS uq_narrative_compass_project;
        DROP INDEX IF EXISTS ix_narrative_compass_project_id;
        DROP TABLE IF EXISTS narrative_compass;
        """
    )
