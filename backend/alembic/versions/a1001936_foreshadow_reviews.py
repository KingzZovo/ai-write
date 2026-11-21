"""Store unconfirmed payoff evidence with source snapshots and retry deduplication."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001936'
down_revision = 'a1001935'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'foreshadow_reviews',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('foreshadow_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('foreshadows.id', ondelete='CASCADE'), nullable=False),
        sa.Column('snapshot_sha256', sa.String(64), nullable=False),
        sa.Column('source_snapshot', sa.JSON(), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('evidence_start', sa.Integer(), nullable=False),
        sa.Column('evidence_end', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('chapter_id', 'foreshadow_id', 'snapshot_sha256', name='uq_foreshadow_review_snapshot'),
    )
    op.create_index('ix_foreshadow_reviews_project_status', 'foreshadow_reviews', ['project_id', 'status', 'created_at'])


def downgrade():
    op.drop_index('ix_foreshadow_reviews_project_status', table_name='foreshadow_reviews')
    op.drop_table('foreshadow_reviews')
