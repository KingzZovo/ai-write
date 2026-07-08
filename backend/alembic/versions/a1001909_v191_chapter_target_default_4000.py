"""v1.9.1 chapter target default 4000

Revision ID: a1001909
Revises: a1001908
Create Date: 2026-06-02

Keep existing rows untouched. Historical rows with ``target_word_count=50000``
are resolved at runtime as the previous default so explicit user values are not
destructively rewritten by migration.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa

revision = "a1001909"
down_revision = "a1001908"
branch_labels = None
depends_on = None


def _column_names(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    if "target_word_count" not in _column_names(conn, "chapters"):
        return
    op.alter_column(
        "chapters",
        "target_word_count",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="4000",
    )


def downgrade() -> None:
    conn = op.get_bind()
    if "target_word_count" not in _column_names(conn, "chapters"):
        return
    op.alter_column(
        "chapters",
        "target_word_count",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="50000",
    )
