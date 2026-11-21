"""Durable payoff-review work for each committed chapter source revision."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001937'
down_revision = 'a1001936'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('foreshadow_review_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_revision', sa.BigInteger(), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('source_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('attempt_token', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('chapter_id', 'source_revision', name='uq_foreshadow_review_job_revision'),
    )
    op.create_index('ix_foreshadow_review_jobs_due', 'foreshadow_review_jobs', ['status', 'updated_at'])


def downgrade():
    op.drop_index('ix_foreshadow_review_jobs_due', table_name='foreshadow_review_jobs')
    op.drop_table('foreshadow_review_jobs')
