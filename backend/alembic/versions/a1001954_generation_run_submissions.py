"""Retain fixed legacy-run requests after their chapter/run is deleted.

Revision ID: a1001954
Revises: a1001953
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001954'
down_revision = 'a1001953'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('generation_run_submissions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        # Deliberately no run/chapter FK: deleting them must not free an
        # accepted request UUID for another model execution.
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('max_rewrite_count', sa.Integer(), nullable=False),
        sa.Column('source_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_generation_run_submissions_project_id', 'generation_run_submissions', ['project_id'])


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Keep the empty check and DDL atomic with concurrent acceptance.
        # Otherwise a request could commit after SELECT and be lost to DROP.
        bind.execute(sa.text('LOCK TABLE generation_run_submissions IN ACCESS EXCLUSIVE MODE'))
    requests = sa.table('generation_run_submissions', sa.column('id', sa.Uuid()))
    if bind.execute(sa.select(requests.c.id).limit(1)).first() is not None:
        raise RuntimeError('已有固定请求回执，须先明确保留数据，不能直接删除防重放证明。')
    op.drop_index('ix_generation_run_submissions_project_id', table_name='generation_run_submissions')
    op.drop_table('generation_run_submissions')
