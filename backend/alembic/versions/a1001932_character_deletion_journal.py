"""Journal committed character cleanup before acknowledging graph receipts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001932'
down_revision = 'a1001931'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('applied_character_deletions',
        sa.Column('token', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    op.drop_table('applied_character_deletions')
