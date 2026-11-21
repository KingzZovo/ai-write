"""Independent task execution ownership and atomic manuscript receipts.

Revision ID: a1001955
Revises: a1001954
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001955'
down_revision = 'a1001954'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('generation_tasks', sa.Column('execution_version', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('generation_tasks', sa.Column('execution_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('generation_tasks', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('generation_tasks', sa.Column('execution_params_json', sa.JSON(), nullable=True))
    op.add_column('generation_tasks', sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('generation_tasks', sa.Column('attempts_json', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('generation_tasks', sa.Column('saved_manuscript_json', sa.JSON(), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        bind.execute(sa.text('LOCK TABLE generation_tasks IN ACCESS EXCLUSIVE MODE'))
    tasks = sa.table('generation_tasks', sa.column('execution_version', sa.Integer()))
    if bind.execute(sa.select(tasks.c.execution_version).where(tasks.c.execution_version != 0).limit(1)).first():
        raise RuntimeError('已有新版独立任务执行及正文保存证明，须先明确保留数据，不能直接删除。')
    for name in ('saved_manuscript_json', 'attempts_json', 'attempt_count', 'execution_params_json',
                 'heartbeat_at', 'execution_id', 'execution_version'):
        op.drop_column('generation_tasks', name)
