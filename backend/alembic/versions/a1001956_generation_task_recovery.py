"""Independent task recovery sources, progress meaning and durable dispatch.

Revision ID: a1001956
Revises: a1001955
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001956'
down_revision = 'a1001955'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('generation_tasks', sa.Column('execution_source_json', sa.JSON(), nullable=True))
    op.add_column('generation_tasks', sa.Column('progress_kind', sa.String(20), nullable=True))
    op.add_column('generation_tasks', sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('generation_tasks', sa.Column('recovery_history_json', sa.JSON(), nullable=False, server_default='[]'))
    op.create_index('ix_generation_tasks_recovery', 'generation_tasks', ['status', 'dispatched_at', 'created_at', 'id'])


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        bind.execute(sa.text('LOCK TABLE generation_tasks IN ACCESS EXCLUSIVE MODE'))
    table = sa.table('generation_tasks', sa.column('execution_source_json', sa.JSON()),
        sa.column('progress_kind', sa.String()), sa.column('dispatched_at', sa.DateTime()),
        sa.column('recovery_history_json', sa.JSON()))
    used = sa.or_(table.c.execution_source_json.is_not(None), table.c.progress_kind.is_not(None),
        table.c.dispatched_at.is_not(None), sa.cast(table.c.recovery_history_json, sa.Text()) != '[]')
    if bind.execute(sa.select(sa.literal(1)).select_from(table).where(used).limit(1)).first():
        raise RuntimeError('已有独立任务恢复来源或派发历史，须先明确保留数据，不能直接删除。')
    op.drop_index('ix_generation_tasks_recovery', table_name='generation_tasks')
    for name in ('recovery_history_json', 'dispatched_at', 'progress_kind', 'execution_source_json'):
        op.drop_column('generation_tasks', name)
