"""Keep full-prose clue candidates and empty-result receipts separate from facts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001942'
down_revision = 'a1001941'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('foreshadow_review_jobs', sa.Column('discovery_count', sa.Integer(), nullable=True))
    op.create_table('foreshadow_discovery_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('source_snapshot', sa.JSON(), nullable=False),
        sa.Column('candidate_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('chapter_id', 'source_sha256', name='uq_foreshadow_discovery_source'),
    )
    op.create_index('ix_foreshadow_discovery_runs_project', 'foreshadow_discovery_runs', ['project_id', 'created_at'])
    op.create_table('foreshadow_discoveries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('foreshadow_discovery_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=False),
        sa.Column('evidence_start', sa.Integer(), nullable=False),
        sa.Column('evidence_end', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('foreshadow_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('handled_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_foreshadow_discoveries_run_status', 'foreshadow_discoveries', ['run_id', 'status', 'created_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM foreshadow_discovery_runs LIMIT 1')).first():
        raise RuntimeError('Preserve discovery evidence and author decisions before downgrading')
    op.drop_index('ix_foreshadow_discoveries_run_status', table_name='foreshadow_discoveries')
    op.drop_table('foreshadow_discoveries')
    op.drop_index('ix_foreshadow_discovery_runs_project', table_name='foreshadow_discovery_runs')
    op.drop_table('foreshadow_discovery_runs')
    op.drop_column('foreshadow_review_jobs', 'discovery_count')
