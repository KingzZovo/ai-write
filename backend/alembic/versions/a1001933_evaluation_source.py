"""Record the source identity of saved chapter evaluations."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001933'
down_revision = 'a1001932'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chapter_evaluations', sa.Column('source_json', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('chapter_evaluations', 'source_json')
