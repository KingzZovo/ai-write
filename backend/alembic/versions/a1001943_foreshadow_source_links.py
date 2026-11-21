"""Retain explicit author associations with saved prose, including unlinks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001943'
down_revision = 'a1001942'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('foreshadow_source_links',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('foreshadow_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('revision', sa.BigInteger(), nullable=False),
        sa.Column('previous_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('command_sha256', sa.String(64), nullable=False),
        sa.Column('operation', sa.String(12), nullable=False),
        sa.Column('record_snapshot', sa.JSON(), nullable=False),
        sa.Column('record_sha256', sa.String(64), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('source_snapshot', sa.JSON(), nullable=True),
        sa.Column('source_sha256', sa.String(64), nullable=True),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('evidence_start', sa.Integer(), nullable=True),
        sa.Column('evidence_end', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'foreshadow_id', 'revision', name='uq_foreshadow_source_link_revision'),
    )


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM foreshadow_source_links LIMIT 1')).first():
        raise RuntimeError('Preserve author source links and unlink decisions before downgrading')
    op.drop_table('foreshadow_source_links')
