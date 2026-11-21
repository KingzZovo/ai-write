"""v1.9: enforce relationships uniqueness on (project_id, source_id, target_id, rel_type)

This supports idempotent entity materialization after rel_type canonicalization.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "a1001902"
down_revision = "a1001901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE: We assume legacy duplicates have been cleaned by scripts/dedupe_relationships_pg.sh.
    op.create_unique_constraint(
        "uq_relationships_rel_key",
        "relationships",
        ["project_id", "source_id", "target_id", "rel_type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_relationships_rel_key", "relationships", type_="unique")

