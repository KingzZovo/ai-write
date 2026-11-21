"""v1.0 BVSR chapter_variants

Revision ID: a1000700
Revises: a0900000
Create Date: 2026-04-22

Adds the `chapter_variants` table used by BVSR (Branch / Variants / Score /
Rank) to store the N-1 candidate drafts that were not chosen as the winner
for a given chapter.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "a1000700"
down_revision = "a0900000"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    return name in set(inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "chapter_variants"):
        return
    op.create_table(
        "chapter_variants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("chapter_id", UUID(as_uuid=True), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("generation_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("variant_idx", sa.Integer, nullable=False),
        sa.Column("content_text", sa.Text, nullable=False),
        sa.Column("word_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("hard_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("soft_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ai_trap_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("critic_report_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_winner", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("selected_by_user", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chapter_variants_chapter_id", "chapter_variants", ["chapter_id"])
    op.create_index("ix_chapter_variants_run_id", "chapter_variants", ["run_id"])
    op.create_unique_constraint(
        "uq_chapter_variants_chapter_run_idx",
        "chapter_variants",
        ["chapter_id", "run_id", "variant_idx"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "chapter_variants"):
        return
    op.drop_constraint("uq_chapter_variants_chapter_run_idx", "chapter_variants", type_="unique")
    op.drop_index("ix_chapter_variants_run_id", table_name="chapter_variants")
    op.drop_index("ix_chapter_variants_chapter_id", table_name="chapter_variants")
    op.drop_table("chapter_variants")
