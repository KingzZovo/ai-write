"""v1.0 usage quotas

Revision ID: a1001200
Revises: a1000800
Create Date: 2026-04-22

Adds the ``usage_quotas`` table used by the 402 quota interceptor and the
``/api/admin/usage`` admin endpoint.

One row per (user_id, month_ym) holds the running monthly token / cost
counters plus the configured quota in cents. ``user_id`` is the JWT
subject (username string), not a UUID -- there is no local ``users`` table.

Table creation is guarded by ``inspect(conn).get_table_names()`` so rerunning
against a database that already has the table is a no-op (DuplicateTable-safe).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa

revision = "a1001200"
down_revision = "a1000800"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    return name in set(inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "usage_quotas"):
        return
    op.create_table(
        "usage_quotas",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("month_ym", sa.String(length=7), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("quota_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "month_ym", name="uq_usage_quotas_user_month"),
    )
    op.create_index(
        "ix_usage_quotas_user_month",
        "usage_quotas",
        ["user_id", "month_ym"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "usage_quotas"):
        return
    op.drop_index("ix_usage_quotas_user_month", table_name="usage_quotas")
    op.drop_constraint(
        "uq_usage_quotas_user_month",
        "usage_quotas",
        type_="unique",
    )
    op.drop_table("usage_quotas")
