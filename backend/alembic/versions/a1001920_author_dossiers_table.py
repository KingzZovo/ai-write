"""Author-level dossier consolidation: author_dossiers table.

One row per reference-book author. The consolidation pipeline
(book_dossier.consolidate_author) merges the per-book dossiers of all
reference_books sharing an ``author`` into one cross-book dossier
(``dossier_json``, mirroring the book dossier contract with larger block
caps). ``status_json`` is the polling marker
({state: queued|running|done|error, updated_at, llm_calls});
``source_book_ids_json`` records which books fed the merge.
"""

from alembic import op

revision = "a1001920"
down_revision = "a1001919"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments (matches a1001919 style).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS author_dossiers (
          id uuid PRIMARY KEY,
          author varchar(200) NOT NULL UNIQUE,
          status_json jsonb,
          dossier_json jsonb,
          source_book_ids_json jsonb,
          created_at timestamptz,
          updated_at timestamptz
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS author_dossiers;")
