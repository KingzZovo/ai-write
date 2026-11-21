"""Bind finite cognition retries to the original Celery delivery."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001946'
down_revision = 'a1001945'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chapter_cognition_extractions', sa.Column('delivery_id', sa.String(100), nullable=True))
    op.add_column('chapter_cognition_extractions', sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('chapter_cognition_extractions', 'attempt_count')
    op.drop_column('chapter_cognition_extractions', 'delivery_id')
