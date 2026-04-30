"""v1.9: add locations table for Neo4j->PG materialization

Neo4j is the source of truth for extracted entities. We materialize
locations into Postgres for fast reads.
"""

from alembic import op
import sqlalchemy as sa


revision = "a1001904"
down_revision = "a1001903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_locations_project_name",
        "locations",
        ["project_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_locations_project_name", "locations", type_="unique")
    op.drop_table("locations")

