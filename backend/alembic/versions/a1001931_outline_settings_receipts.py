"""Persist validated outline settings before graph/projection delivery."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'a1001931'
down_revision = 'a1001930'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('outline_settings_extractions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('outline_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('outlines.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('payload_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('counts_json', sa.JSON()),
        sa.Column('last_error', sa.String(128)),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('outline_id', 'source_hash', name='uq_outline_settings_source'))
    op.create_index('ix_outline_settings_retry', 'outline_settings_extractions', ['status', 'next_attempt_at'])


def downgrade():
    op.drop_table('outline_settings_extractions')
