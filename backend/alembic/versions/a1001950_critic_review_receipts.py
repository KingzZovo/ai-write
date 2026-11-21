"""Persist critic delivery metadata without certifying legacy empty reports.

Revision ID: a1001950
Revises: a1001949
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001950'
down_revision = 'a1001949'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('critic_reports', sa.Column('review_json', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    reports = sa.table('critic_reports', sa.column('review_json', sa.JSON()))
    has_receipts = op.get_bind().execute(
        sa.select(reports.c.review_json).where(sa.cast(reports.c.review_json, sa.Text()) != '{}').limit(1)
    ).first()
    if has_receipts is not None:
        raise RuntimeError('已有评审回执；请先备份并显式迁移，不能直接删除评审完整性记录。')
    op.drop_column('critic_reports', 'review_json')
