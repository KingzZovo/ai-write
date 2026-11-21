"""Source-bound cognition changes; legacy cumulative rows remain untouched."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'a1001929'
down_revision = 'a1001928'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('chapter_cognition_extractions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('source_json', sa.JSON(), nullable=False),
        sa.Column('changes_json', sa.JSON()),
        sa.Column('payload_hash', sa.String(64)),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('last_error', sa.String(128)),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('chapter_id','source_hash',name='uq_chapter_cognition_source'))
    op.create_index('ix_chapter_cognition_retry','chapter_cognition_extractions',['status','updated_at'])
    op.create_index('ix_chapter_cognition_project','chapter_cognition_extractions',['project_id'])

def downgrade():
    op.drop_table('chapter_cognition_extractions')
