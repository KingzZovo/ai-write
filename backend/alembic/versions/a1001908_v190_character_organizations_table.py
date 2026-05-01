"""v1.9: add character_organizations table for MEMBER_OF projection

Neo4j is the source of truth for entity timelines. We materialize the
Character-MEMBER_OF->Organization edges into Postgres for fast reads.
"""

from alembic import op


revision = "a1001908"
down_revision = "a1001907"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_organizations (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_id uuid NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
          organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
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
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_organizations_key'
          ) THEN
            ALTER TABLE character_organizations
              ADD CONSTRAINT uq_character_organizations_key
              UNIQUE (project_id, character_id, organization_id, chapter_start);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_organizations
          DROP CONSTRAINT IF EXISTS uq_character_organizations_key;
        DROP TABLE IF EXISTS character_organizations;
        """
    )

