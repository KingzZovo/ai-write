"""Bind volume memory to its chapter sources; legacy memory stays unknown."""
from alembic import op

revision = 'a1001923'
down_revision = 'a1001922'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE volume_summaries ADD COLUMN IF NOT EXISTS source_manifest json')
    op.execute('ALTER TABLE volume_summaries ADD COLUMN IF NOT EXISTS source_hash varchar(64)')


def downgrade():
    op.execute('ALTER TABLE volume_summaries DROP COLUMN IF EXISTS source_hash')
    op.execute('ALTER TABLE volume_summaries DROP COLUMN IF EXISTS source_manifest')
