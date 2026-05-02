"""v1.5.0 — outline level=book uniqueness + from_reference rename

Revision ID: a1001500
Revises: a1001401
Create Date: 2026-04-26

Establishes the invariant that each project has at most one book-level
outline. Previously the outlines table had no uniqueness on
(project_id, level), and worker-side auto-save derived `level` from
task_type via naive string ops, producing semantically wrong levels
like `from_reference`.

Changes:
1. Data: rename `level='from_reference'` -> `level='book'` (semantic
   correction; from_reference outlines ARE book-level outlines built
   from a reference book, just with a different generation method).
2. Data: collapse stale book outlines per project, keeping the most
   recently created row (ties broken by id desc).
3. Schema: partial UNIQUE index on (project_id) WHERE level='book'.

After upgrade, every project has 0 or 1 row with level='book'. Volume
outlines (level='volume') remain N rows per project, distinguished by
parent_id + content_json.volume_idx; the partial index does not affect
them.

Downgrade drops the unique index. Renamed/collapsed rows are NOT
restored — the data unification is permanent (those rows would have
been broken under the old query paths anyway).
"""
from alembic import op

revision = "a1001500"
down_revision = "a1001401"
branch_labels = None
depends_on = None


def upgrade():
    # Step 1: rename from_reference -> book (semantic unification).
    op.execute(
        """
        UPDATE outlines
        SET level = 'book'
        WHERE level = 'from_reference'
        """
    )

    # Step 2: collapse stale book rows — keep most recent per project,
    # delete older duplicates. Uses ROW_NUMBER() OVER (PARTITION BY
    # project_id ORDER BY created_at DESC, id DESC).
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY project_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM outlines
            WHERE level = 'book'
        )
        DELETE FROM outlines
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    # Step 3: enforce the invariant at schema level.
    op.execute(
        """
        CREATE UNIQUE INDEX outlines_project_book_unique
        ON outlines (project_id)
        WHERE level = 'book'
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS outlines_project_book_unique")
