"""v1.9: add character_locations table for AT_LOCATION projection

Neo4j is the source of truth for entity timelines. We materialize the
Character-AT_LOCATION->Location edges into Postgres for fast reads and
checker services.
"""

from alembic import op


revision = "a1001905"
down_revision = "a1001904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_locations (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_id uuid NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
          location_id uuid NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
          chapter_start integer NOT NULL,
          chapter_end integer,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_locations_key'
          ) THEN
            ALTER TABLE character_locations
              ADD CONSTRAINT uq_character_locations_key
              UNIQUE (project_id, character_id, location_id, chapter_start);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_locations
          DROP CONSTRAINT IF EXISTS uq_character_locations_key;
        DROP TABLE IF EXISTS character_locations;
        """
    )

