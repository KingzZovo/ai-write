"""Remember direct author writes after source deletion and lost responses."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001941'
down_revision = 'a1001940'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('foreshadow_write_receipts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('foreshadow_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('command_sha256', sa.String(64), nullable=False),
        sa.Column('operation', sa.String(16), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('outcome', sa.String(12), nullable=False),
        sa.Column('reason', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_foreshadow_writes_project', 'foreshadow_write_receipts', ['project_id', 'created_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM foreshadow_write_receipts LIMIT 1')).first():
        raise RuntimeError('Preserve author write receipts before downgrading; old retries must not be replayed')
    op.drop_index('ix_foreshadow_writes_project', table_name='foreshadow_write_receipts')
    op.drop_table('foreshadow_write_receipts')
