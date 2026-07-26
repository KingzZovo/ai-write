"""Book-global chapter index: chapters.global_idx.

``chapters.chapter_idx`` is volume-local (1-based per volume,
api/volumes.py materializes ``i + 1``), but half the memory subsystem
compared it project-wide: extraction markers collided across volumes,
timeline/strand queries interleaved volumes, and
character_states/character_locations.chapter_start had no single
monotonic axis to live on.

This migration adds the canonical book-global axis:

    global_idx = (count of chapters in volumes with lower volume_idx)
                 + chapter_idx

The convention is identical to the one already used for
``foreshadows.planted_chapter`` (see
``foreshadow_lifecycle.chapter_global_idx``), so for single-volume
projects ``global_idx == chapter_idx`` and nothing changes.

Backfill recomputes for every existing chapter (safe to re-run). New
chapters are stamped by the ``before_insert`` listener in
``app/models/project.py``.
"""

from alembic import op

revision = "a1001915"
down_revision = "a1001914"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE chapters ADD COLUMN IF NOT EXISTS global_idx integer;
        """
    )
    op.execute(
        """
        WITH vol_counts AS (
            SELECT v.id AS volume_id, v.project_id, v.volume_idx,
                   COUNT(c.id) AS cnt
            FROM volumes v
            LEFT JOIN chapters c ON c.volume_id = v.id
            GROUP BY v.id, v.project_id, v.volume_idx
        ),
        bases AS (
            SELECT volume_id,
                   COALESCE(
                       SUM(cnt) OVER (
                           PARTITION BY project_id
                           ORDER BY volume_idx
                           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ), 0
                   ) AS base
            FROM vol_counts
        )
        UPDATE chapters
        SET global_idx = chapters.chapter_idx + bases.base
        FROM bases
        WHERE chapters.volume_id = bases.volume_id;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chapters_global_idx
        ON chapters (global_idx);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chapters_global_idx;")
    op.execute("ALTER TABLE chapters DROP COLUMN IF EXISTS global_idx;")
