"""Durable chapter extraction payloads and fencing sequence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'a1001924'
down_revision = 'a1001923'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('CREATE SEQUENCE IF NOT EXISTS entity_projection_tokens')
    op.create_table('chapter_entity_extractions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('source_json', sa.JSON(), nullable=False),
        sa.Column('payload_json', sa.JSON()), sa.Column('payload_hash', sa.String(64)),
        sa.Column('projection_token', sa.BigInteger(), server_default=sa.text("nextval('entity_projection_tokens')"), nullable=False),
        sa.Column('status', sa.String(24), nullable=False), sa.Column('last_error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('chapter_id', 'source_hash', name='uq_chapter_entity_source'),
    )
    op.create_index('ix_entity_extraction_retry', 'chapter_entity_extractions', ['status', 'updated_at'])

def downgrade():
    op.drop_table('chapter_entity_extractions')
    op.execute('DROP SEQUENCE IF EXISTS entity_projection_tokens')
