"""v1.9: add locations table for Neo4j->PG materialization

Neo4j is the source of truth for extracted entities. We materialize
locations into Postgres for fast reads.
"""

from alembic import op


revision = "a1001904"
down_revision = "a1001903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: local dev environments may already have the table/constraint.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS locations (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name varchar(200) NOT NULL,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_locations_project_name'
          ) THEN
            ALTER TABLE locations
              ADD CONSTRAINT uq_locations_project_name
              UNIQUE (project_id, name);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_locations_project_name", "locations", type_="unique")
    op.drop_table("locations")

