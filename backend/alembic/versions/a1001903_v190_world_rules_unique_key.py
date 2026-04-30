"""v1.9: enforce world_rules uniqueness on (project_id, category, rule_text)

Neo4j is the source of truth for extracted entities. We materialize world
rules into Postgres for fast reads; a DB-level unique constraint supports
idempotent materialization.
"""

from alembic import op


revision = "a1001903"
down_revision = "a1001902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: local dev environments may have applied the constraint manually.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_world_rules_key'
          ) THEN
            ALTER TABLE world_rules
              ADD CONSTRAINT uq_world_rules_key
              UNIQUE (project_id, category, rule_text);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_world_rules_key", "world_rules", type_="unique")

