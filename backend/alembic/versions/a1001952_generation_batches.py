"""Persist fixed batch submissions and ordered chapter results.

Revision ID: a1001952
Revises: a1001951
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001952'
down_revision = 'a1001951'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('generation_batches',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('total_chapters', sa.Integer(), nullable=False),
        sa.Column('worker_id', sa.UUID(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_generation_batches_project', 'generation_batches', ['project_id', 'created_at', 'id'])
    op.create_table('generation_batch_items',
        sa.Column('task_id', sa.UUID(), primary_key=True),
        sa.Column('batch_id', sa.UUID(), sa.ForeignKey('generation_batches.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), nullable=False),
        sa.Column('volume_id', sa.UUID(), nullable=False),
        sa.Column('volume_idx', sa.Integer(), nullable=False),
        sa.Column('chapter_idx', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('outline_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('word_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.UniqueConstraint('batch_id', 'position', name='uq_generation_batch_position'))


def downgrade():
    batches = sa.table('generation_batches', sa.column('id', sa.UUID()))
    if op.get_bind().execute(sa.select(batches.c.id).limit(1)).first() is not None:
        raise RuntimeError('已有批量生成请求，请先完成明确的数据保留迁移，不能直接删除进度与回执。')
    op.drop_table('generation_batch_items')
    op.drop_table('generation_batches')
