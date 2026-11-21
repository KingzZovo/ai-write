"""Record the exact chapter source and summary output identity."""
from alembic import op

revision = "a1001922"
down_revision = "a1001921"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Never stamp pre-existing summaries using today's text: their actual
    # source is unknown and may predate an author edit or originate in an outline.
    op.execute("ALTER TABLE chapters ADD COLUMN IF NOT EXISTS summary_source_hash varchar(64)")


def downgrade() -> None:
    op.execute("ALTER TABLE chapters DROP COLUMN IF EXISTS summary_source_hash")
