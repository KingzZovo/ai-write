"""Foreshadow tracking chain repair: source column + 'pending' status fix.

Two related defects:

1. foreshadow_lifecycle inserted rows with status='pending', but every
   consumer filters on ('planted','ripening','ready') — those rows were
   invisible to 【伏笔追踪】 forever. Data fix: pending -> planted. Only
   foreshadow_lifecycle ever wrote 'pending', so the rows are also
   back-stamped source='lifecycle'.

2. The entity materialize deletion sync treated Neo4j as source of truth
   for ALL PG foreshadow rows and wiped PG-only rows on every extraction
   run. New nullable `source` column distinguishes origin: 'neo4j' rows
   are stamped by the materialize upsert and are the only ones eligible
   for deletion sync; 'lifecycle' / NULL rows are never deleted by it.
"""

from alembic import op

revision = "a1001914"
down_revision = "a1001913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE foreshadows ADD COLUMN IF NOT EXISTS source varchar(20);
        """
    )
    op.execute(
        """
        UPDATE foreshadows
        SET status = 'planted', source = 'lifecycle'
        WHERE status = 'pending';
        """
    )


def downgrade() -> None:
    # Status data fix is intentionally not reverted ('pending' was a bug).
    op.execute(
        """
        ALTER TABLE foreshadows DROP COLUMN IF EXISTS source;
        """
    )
