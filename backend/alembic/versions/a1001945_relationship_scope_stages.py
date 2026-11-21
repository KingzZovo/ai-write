"""Keep chapter extraction and complete relationship-review batches resumable."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001945'
down_revision = 'a1001944'
branch_labels = None
depends_on = None


def upgrade():
    # Existing payloads already use the complete, legacy digest contract.
    op.add_column('chapter_entity_extractions', sa.Column(
        'payload_stage', sa.String(24), nullable=False, server_default='complete'))
    op.add_column('chapter_entity_extractions', sa.Column('scope_json', sa.JSON(none_as_null=True), nullable=True))
    op.add_column('chapter_entity_extractions', sa.Column('delivery_id', sa.String(100), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM chapter_entity_extractions "
        "WHERE payload_stage <> 'complete' OR scope_json IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError('Preserve unfinished relationship reviews before downgrading')
    op.drop_column('chapter_entity_extractions', 'delivery_id')
    op.drop_column('chapter_entity_extractions', 'scope_json')
    op.drop_column('chapter_entity_extractions', 'payload_stage')
