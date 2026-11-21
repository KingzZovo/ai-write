"""v1.9.1 Q3: character_cognitions table (per-character knowledge ledger)

Tracks what each character knows / does not know, plus a global reader
perspective row (character_name='__reader__', knows only). The gap between
reader-known and character-unknown facts is the basis of suspense; the
evaluator uses this ledger to flag cognition_violation issues (a character
acting on information it never obtained).

Adapted from QMAI character-cognition (MIT, github.com/Mochocyang/QMAI).
"""

from alembic import op

revision = "a1001910"
down_revision = "a1001909"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_cognitions (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_name varchar(128) NOT NULL,
          knows jsonb,
          does_not_know jsonb,
          created_at timestamptz,
          updated_at timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_character_cognitions_project_id
          ON character_cognitions (project_id);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_cognitions_key'
          ) THEN
            ALTER TABLE character_cognitions
              ADD CONSTRAINT uq_character_cognitions_key
              UNIQUE (project_id, character_name);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_cognitions
          DROP CONSTRAINT IF EXISTS uq_character_cognitions_key;
        DROP INDEX IF EXISTS ix_character_cognitions_project_id;
        DROP TABLE IF EXISTS character_cognitions;
        """
    )
