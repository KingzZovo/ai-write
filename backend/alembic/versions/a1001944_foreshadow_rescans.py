"""Preserve scan lists, shared author decisions and fixed submission receipts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001944'
down_revision = 'a1001943'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('foreshadow_review_jobs', sa.Column('rescan_discovery', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('foreshadow_review_jobs', sa.Column('discovery_run_id', postgresql.UUID(as_uuid=True), nullable=True))
    with op.batch_alter_table('foreshadow_discovery_runs') as batch:
        batch.add_column(sa.Column('scan_revision', sa.BigInteger(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('model_name', sa.String(200), nullable=True))
        batch.drop_constraint('uq_foreshadow_discovery_source', type_='unique')
        batch.create_unique_constraint('uq_foreshadow_discovery_scan', ['chapter_id', 'source_sha256', 'scan_revision'])
    op.create_table('foreshadow_discovery_run_items',
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('foreshadow_discovery_runs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('candidate_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('foreshadow_discoveries.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.UniqueConstraint('run_id', 'position', name='uq_foreshadow_discovery_item_position'),
    )
    # Keep original IDs, evidence, explanations and decisions byte for byte.
    op.execute(sa.text('''INSERT INTO foreshadow_discovery_run_items (run_id, candidate_id, position, reason)
        SELECT run_id, id, ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY created_at, id) - 1, reason
        FROM foreshadow_discoveries'''))
    op.create_index('ix_foreshadow_discovery_items_candidate', 'foreshadow_discovery_run_items', ['candidate_id'])
    op.create_table('foreshadow_review_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('job_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rescan_discovery', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_foreshadow_review_requests_project', 'foreshadow_review_requests', ['project_id'])


def downgrade():
    connection = op.get_bind()
    # Do not silently discard author request receipts, extra scans or their
    # ordering/explanations. Empty legacy scans are representable by 1943.
    for query in (
        'SELECT 1 FROM foreshadow_review_requests LIMIT 1',
        'SELECT 1 FROM foreshadow_discovery_runs WHERE scan_revision <> 0 OR model_name IS NOT NULL LIMIT 1',
        'SELECT 1 FROM foreshadow_review_jobs WHERE rescan_discovery OR discovery_run_id IS NOT NULL LIMIT 1',
    ):
        if connection.execute(sa.text(query)).first():
            raise RuntimeError('Preserve rescan evidence and request receipts before downgrading')
    op.drop_table('foreshadow_review_requests')
    op.drop_table('foreshadow_discovery_run_items')
    with op.batch_alter_table('foreshadow_discovery_runs') as batch:
        batch.drop_constraint('uq_foreshadow_discovery_scan', type_='unique')
        batch.create_unique_constraint('uq_foreshadow_discovery_source', ['chapter_id', 'source_sha256'])
        batch.drop_column('scan_revision')
        batch.drop_column('model_name')
    op.drop_column('foreshadow_review_jobs', 'discovery_run_id')
    op.drop_column('foreshadow_review_jobs', 'rescan_discovery')
