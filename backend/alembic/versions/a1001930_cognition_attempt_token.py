"""Fence late cognition outcomes after connection lease loss."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'a1001930'
down_revision = 'a1001929'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('chapter_cognition_extractions', sa.Column('attempt_token', postgresql.UUID(as_uuid=True), nullable=True))

def downgrade():
    op.drop_column('chapter_cognition_extractions', 'attempt_token')
