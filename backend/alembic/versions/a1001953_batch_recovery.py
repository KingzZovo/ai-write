"""Add batch execution provenance and atomic manuscript save receipts.

Revision ID: a1001953
Revises: a1001952
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001953'
down_revision = 'a1001952'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('generation_batches', sa.Column('execution_version', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('generation_batches', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('generation_batches', sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('generation_batches', sa.Column('recovery_count', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_generation_batches_recovery', 'generation_batches',
                    ['status', 'execution_version', 'heartbeat_at', 'dispatched_at'])
    op.add_column('generation_batch_items', sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('generation_batch_items', sa.Column('attempts_json', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('generation_batch_items', sa.Column('saved_receipt_json', sa.JSON(), nullable=True))


def downgrade():
    batches = sa.table('generation_batches', sa.column('execution_version', sa.Integer()))
    if op.get_bind().execute(sa.select(batches.c.execution_version).where(
            batches.c.execution_version != 0).limit(1)).first() is not None:
        raise RuntimeError('已有新版批次执行及正文保存证明，须先明确保留数据，不能直接删去恢复记录。')
    for name in ('saved_receipt_json', 'attempts_json', 'attempt_count'):
        op.drop_column('generation_batch_items', name)
    op.drop_index('ix_generation_batches_recovery', table_name='generation_batches')
    for name in ('recovery_count', 'dispatched_at', 'heartbeat_at', 'execution_version'):
        op.drop_column('generation_batches', name)
