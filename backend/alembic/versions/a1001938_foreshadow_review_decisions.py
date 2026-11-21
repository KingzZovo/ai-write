"""Record the first author decision without inventing legacy history."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001938'
down_revision = 'a1001937'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('foreshadow_reviews', sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('foreshadow_reviews', sa.Column('handled_by', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('foreshadow_reviews', 'handled_by')
    op.drop_column('foreshadow_reviews', 'handled_at')
