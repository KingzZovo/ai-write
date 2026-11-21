"""v1.9: add organizations table for Neo4j->PG materialization

Neo4j is the source of truth for extracted entities. We materialize
organizations into Postgres for fast reads (context pack / memory) and to
support future checker services.
"""

from alembic import op


revision = "a1001907"
down_revision = "a1001906"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: local dev environments may already have the table/constraint.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
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
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_organizations_project_name'
          ) THEN
            ALTER TABLE organizations
              ADD CONSTRAINT uq_organizations_project_name
              UNIQUE (project_id, name);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS organizations
          DROP CONSTRAINT IF EXISTS uq_organizations_project_name;
        DROP TABLE IF EXISTS organizations;
        """
    )

