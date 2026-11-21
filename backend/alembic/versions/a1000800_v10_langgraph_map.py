"""v1.0 LangGraph thread map

Revision ID: a1000800
Revises: a1000700
Create Date: 2026-04-22

Maps a ``generation_runs.id`` to a LangGraph ``thread_id`` so the same run
can be resumed across processes. LangGraph's own checkpoint tables
(``langgraph_checkpoints``, ``langgraph_checkpoint_blobs``,
``langgraph_checkpoint_writes``) are created by the checkpointer's
``setup()`` call — not here.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a1000800"
down_revision = "a1000700"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    return name in set(inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "langgraph_thread_map"):
        return
    op.create_table(
        "langgraph_thread_map",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("generation_run_id", UUID(as_uuid=True), sa.ForeignKey("generation_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("thread_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("graph_name", sa.String(length=64), nullable=False, server_default="generation_graph"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_langgraph_thread_map_thread_id", "langgraph_thread_map", ["thread_id"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "langgraph_thread_map"):
        return
    op.drop_index("ix_langgraph_thread_map_thread_id", table_name="langgraph_thread_map")
    op.drop_table("langgraph_thread_map")
