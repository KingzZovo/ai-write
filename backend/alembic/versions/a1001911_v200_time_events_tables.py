"""v2.0 (PR-NEO3): time_anchors + chapter_time_anchors tables

Neo4j is the source of truth for narrative time anchors:
  (:Time {project_id, label, kind, abs_value?})
  (:Chapter)-[:OCCURS_AT {precision}]->(:Time)

Kinds: era / festival / anniversary / day_offset / absolute.

Postgres mirrors them so the time_reversal checker can quickly look up
the last known time anchor before drafting a new chapter.

Note: we don't FK to a Postgres `chapters` table for the chapter linkage
because the source of truth is (project_id, chapter_idx), which is the
stable identifier for narrative chapters. This avoids joining from the
Neo4j Chapter id to PG chapter rows during materialization.

Idempotent CREATE TABLE IF NOT EXISTS pattern.
"""

from alembic import op


revision = "a1001911"
down_revision = "a1001910"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS time_anchors (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          label varchar(200) NOT NULL,
          kind varchar(40) NOT NULL,
          abs_value bigint,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_time_anchors_key'
          ) THEN
            ALTER TABLE time_anchors
              ADD CONSTRAINT uq_time_anchors_key
              UNIQUE (project_id, label, kind);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chapter_time_anchors (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          chapter_idx integer NOT NULL,
          time_anchor_id uuid NOT NULL REFERENCES time_anchors(id) ON DELETE CASCADE,
          precision varchar(20) DEFAULT '',
          offset_value integer,
          anchor_label varchar(200) DEFAULT '',
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_chapter_time_anchors_key'
          ) THEN
            ALTER TABLE chapter_time_anchors
              ADD CONSTRAINT uq_chapter_time_anchors_key
              UNIQUE (project_id, chapter_idx, time_anchor_id);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chapter_time_anchors_project_chapter "
        "ON chapter_time_anchors (project_id, chapter_idx);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chapter_time_anchors_project_chapter;")
    op.execute(
        "ALTER TABLE IF EXISTS chapter_time_anchors "
        "DROP CONSTRAINT IF EXISTS uq_chapter_time_anchors_key;"
    )
    op.execute("DROP TABLE IF EXISTS chapter_time_anchors;")
    op.execute(
        "ALTER TABLE IF EXISTS time_anchors "
        "DROP CONSTRAINT IF EXISTS uq_time_anchors_key;"
    )
    op.execute("DROP TABLE IF EXISTS time_anchors;")
