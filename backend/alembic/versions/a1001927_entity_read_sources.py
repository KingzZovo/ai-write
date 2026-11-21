"""Carry chapter provenance to entity read projections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'a1001927'
down_revision = 'a1001926'
branch_labels = None
depends_on = None


def upgrade():
    for table in ('character_states', 'character_locations'):
        op.add_column(table, sa.Column('source_chapter_id', postgresql.UUID(as_uuid=True)))
        op.add_column(table, sa.Column('source_hash', sa.String(64)))


def downgrade():
    for table in ('character_states', 'character_locations'):
        op.drop_column(table, 'source_hash')
        op.drop_column(table, 'source_chapter_id')
