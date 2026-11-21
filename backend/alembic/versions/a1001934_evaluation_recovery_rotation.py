"""Rotate evaluation recovery scans independently of worker progress."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001934'
down_revision = 'a1001933'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('evaluate_tasks', sa.Column('recovery_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_evaluate_tasks_recovery_rotation', 'evaluate_tasks',
                    [sa.text('coalesce(recovery_checked_at, created_at)'), 'id'],
                    postgresql_where=sa.text("status IN ('pending', 'running')"))


def downgrade():
    op.drop_index('ix_evaluate_tasks_recovery_rotation', table_name='evaluate_tasks')
    op.drop_column('evaluate_tasks', 'recovery_checked_at')
