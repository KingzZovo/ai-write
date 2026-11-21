"""Distinguish chapter-local state changes from legacy merged snapshots."""
from alembic import op
import sqlalchemy as sa
revision = 'a1001928'
down_revision = 'a1001927'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('character_states', sa.Column('source_mode', sa.String(32)))
    op.add_column('chapter_entity_extractions', sa.Column('projection_version', sa.Integer(), nullable=False, server_default='1'))

def downgrade():
    op.drop_column('chapter_entity_extractions', 'projection_version')
    op.drop_column('character_states', 'source_mode')
