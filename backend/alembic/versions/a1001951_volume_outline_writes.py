"""Keep author volume-outline history and fixed submission receipts.

Revision ID: a1001951
Revises: a1001950
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001951'
down_revision = 'a1001950'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('volume_outline_writes',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('volume_id', sa.UUID(), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('before_json', sa.JSON(), nullable=False),
        sa.Column('result_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_volume_outline_write_history', 'volume_outline_writes',
        ['project_id', 'volume_id', 'created_at', 'id'])


def downgrade():
    rows = sa.table('volume_outline_writes', sa.column('id', sa.UUID()))
    if op.get_bind().execute(sa.select(rows.c.id).limit(1)).first() is not None:
        raise RuntimeError('已有作者卷纲保存历史，请先完成明确的数据保留迁移，不能直接删除回执。')
    op.drop_table('volume_outline_writes')
