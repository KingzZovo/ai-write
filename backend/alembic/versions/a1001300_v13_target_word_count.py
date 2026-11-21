"""v1.3 target_word_count across Project/Volume/Chapter

Revision ID: a1001300
Revises: a1001200
Create Date: 2026-04-23

chunk-28 of v1.3.0 C series. Introduces the top-level word-count budget
hierarchy:

- ``projects.target_word_count``   INT NOT NULL default 3_000_000
- ``volumes.target_word_count``    INT NOT NULL default 200_000
- ``chapters.target_word_count``   INT NOT NULL default 50_000 (renamed from
  the pre-existing nullable ``target_words`` column; existing NULLs are
  backfilled to the default before applying NOT NULL)

All inspections are guarded so re-running is idempotent.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa

revision = "a1001300"
down_revision = "a1001200"
branch_labels = None
depends_on = None


PROJECT_DEFAULT = 3_000_000
VOLUME_DEFAULT = 200_000
CHAPTER_DEFAULT = 50_000


def _column_names(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()

    # --- chapters: rename target_words -> target_word_count, backfill, NOT NULL ---
    cols = _column_names(conn, "chapters")
    if "target_words" in cols and "target_word_count" not in cols:
        op.alter_column(
            "chapters",
            "target_words",
            new_column_name="target_word_count",
            existing_type=sa.Integer(),
            existing_nullable=True,
        )
    elif "target_word_count" not in cols:
        op.add_column(
            "chapters",
            sa.Column(
                "target_word_count",
                sa.Integer(),
                nullable=True,
            ),
        )

    # Backfill any NULLs with the chapter default so NOT NULL is safe.
    op.execute(
        sa.text(
            "UPDATE chapters SET target_word_count = :d "
            "WHERE target_word_count IS NULL"
        ).bindparams(d=CHAPTER_DEFAULT)
    )
    op.alter_column(
        "chapters",
        "target_word_count",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=str(CHAPTER_DEFAULT),
    )

    # --- projects: add target_word_count NOT NULL default 3_000_000 ---
    if "target_word_count" not in _column_names(conn, "projects"):
        op.add_column(
            "projects",
            sa.Column(
                "target_word_count",
                sa.Integer(),
                nullable=False,
                server_default=str(PROJECT_DEFAULT),
            ),
        )

    # --- volumes: add target_word_count NOT NULL default 200_000 ---
    if "target_word_count" not in _column_names(conn, "volumes"):
        op.add_column(
            "volumes",
            sa.Column(
                "target_word_count",
                sa.Integer(),
                nullable=False,
                server_default=str(VOLUME_DEFAULT),
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if "target_word_count" in _column_names(conn, "volumes"):
        op.drop_column("volumes", "target_word_count")

    if "target_word_count" in _column_names(conn, "projects"):
        op.drop_column("projects", "target_word_count")

    cols = _column_names(conn, "chapters")
    if "target_word_count" in cols:
        op.alter_column(
            "chapters",
            "target_word_count",
            existing_type=sa.Integer(),
            nullable=True,
            server_default=None,
        )
        if "target_words" not in cols:
            op.alter_column(
                "chapters",
                "target_word_count",
                new_column_name="target_words",
                existing_type=sa.Integer(),
                existing_nullable=True,
            )
