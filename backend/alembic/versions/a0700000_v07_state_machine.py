"""v0.7: generation_runs + critic_reports (state machine + critic).

Revision ID: a0700000
Revises: a0600000
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a0700000"
down_revision = "a0600000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chapter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="planning"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("checkpoint_data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rewrite_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_rewrite_count", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_generation_runs_project_id", "generation_runs", ["project_id"])
    op.create_index("ix_generation_runs_chapter_id", "generation_runs", ["chapter_id"])
    op.create_index("ix_generation_runs_status", "generation_runs", ["status"])

    op.create_table(
        "critic_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("generation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issues_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_critic_reports_run_id", "critic_reports", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_critic_reports_run_id", table_name="critic_reports")
    op.drop_table("critic_reports")
    op.drop_index("ix_generation_runs_status", table_name="generation_runs")
    op.drop_index("ix_generation_runs_chapter_id", table_name="generation_runs")
    op.drop_index("ix_generation_runs_project_id", table_name="generation_runs")
    op.drop_table("generation_runs")
