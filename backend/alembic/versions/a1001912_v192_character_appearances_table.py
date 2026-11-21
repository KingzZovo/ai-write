"""v1.9.2 C3/F4: character_appearances table (secondary-cast roster)

One row per (project, character_name): first/last appearance chapter
(book-global idx) + appearance count. Populated by the deterministic recompute
task. Used to remind generation to re-read a long-absent character, and as the
"character last seen" signal for deterministic related-chapter recall.

Adapted from voocel/ainovel-cli cast tracking (design idea; wording our own).
"""

from alembic import op

revision = "a1001912"
down_revision = "a1001911"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_appearances (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_name varchar(128) NOT NULL,
          first_seen_chapter integer,
          last_seen_chapter integer,
          appearance_count integer,
          updated_at timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_character_appearances_project_id
          ON character_appearances (project_id);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_appearances_key'
          ) THEN
            ALTER TABLE character_appearances
              ADD CONSTRAINT uq_character_appearances_key
              UNIQUE (project_id, character_name);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_appearances
          DROP CONSTRAINT IF EXISTS uq_character_appearances_key;
        DROP INDEX IF EXISTS ix_character_appearances_project_id;
        DROP TABLE IF EXISTS character_appearances;
        """
    )
