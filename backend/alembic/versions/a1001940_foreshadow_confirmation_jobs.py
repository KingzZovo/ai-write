"""Durable author intents for graph-owned payoff confirmation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001940'
down_revision = 'a1001939'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('foreshadow_confirmation_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), sa.ForeignKey('foreshadow_reviews.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('requested_by', sa.Text(), nullable=True),
        sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('attempt_token', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('result_json', sa.JSON(), nullable=True),
        sa.Column('last_error', sa.String(128), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_foreshadow_confirmations_due', 'foreshadow_confirmation_jobs', ['status', 'updated_at'])


def downgrade():
    pending = op.get_bind().execute(sa.text("SELECT 1 FROM foreshadow_confirmation_jobs j JOIN foreshadow_reviews r ON r.id=j.id WHERE j.status NOT IN ('completed','rejected') OR r.status='confirming' LIMIT 1")).first()
    if pending:
        raise RuntimeError('Settle outstanding graph confirmations before dropping their recovery records')
    op.drop_index('ix_foreshadow_confirmations_due', table_name='foreshadow_confirmation_jobs')
    op.drop_table('foreshadow_confirmation_jobs')
