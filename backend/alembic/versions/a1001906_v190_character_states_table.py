"""v1.9: add character_states table for HAS_STATE projection

Neo4j is the source of truth for entity timelines. We materialize the
Character-HAS_STATE->CharacterState nodes into Postgres for fast reads in
checker services and generation pre-hooks.
"""

from alembic import op

revision = "a1001906"
down_revision = "a1001905"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_states (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_id uuid NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
          chapter_start integer NOT NULL,
          chapter_end integer,
          status_json jsonb,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_states_key'
          ) THEN
            ALTER TABLE character_states
              ADD CONSTRAINT uq_character_states_key
              UNIQUE (project_id, character_id, chapter_start);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_states
          DROP CONSTRAINT IF EXISTS uq_character_states_key;
        DROP TABLE IF EXISTS character_states;
        """
    )
