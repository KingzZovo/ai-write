"""Fence author retries and retain history backfill handoff progress."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001947'
down_revision = 'a1001946'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chapter_cognition_extractions', sa.Column('cycle_id', sa.UUID(), nullable=True))
    op.execute('UPDATE chapter_cognition_extractions SET cycle_id = id')
    with op.batch_alter_table('chapter_cognition_extractions') as batch:
        batch.alter_column('cycle_id', existing_type=sa.UUID(), nullable=False)
    op.create_table('cognition_requests',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('result_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('cognition_backfills',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_ids', sa.JSON(), nullable=False),
        sa.Column('chapter_count', sa.Integer(), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=False, server_default='0') for name in
          ('cursor', 'added_count', 'existing_count', 'attention_count', 'unavailable_count')),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('uq_cognition_backfill_active', 'cognition_backfills', ['project_id'], unique=True,
        postgresql_where=sa.text("status = 'pending'"), sqlite_where=sa.text("status = 'pending'"))
    op.create_index('ix_cognition_backfill_recovery', 'cognition_backfills', ['status', 'updated_at'])


def downgrade():
    op.drop_table('cognition_backfills')
    op.drop_table('cognition_requests')
    op.drop_column('chapter_cognition_extractions', 'cycle_id')
