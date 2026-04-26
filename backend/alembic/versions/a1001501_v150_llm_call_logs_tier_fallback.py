"""v1.5.0 B1 - llm_call_logs add tier_used + fallback_reason + attempt_index

Revision ID: a1001501
Revises: a1001500
Create Date: 2026-04-26

"""
from alembic import op
import sqlalchemy as sa


revision = "a1001501"
down_revision = "a1001500"
branch_labels = None
depends_on = None


VALID_TIERS = ("flagship", "standard", "small", "distill", "embedding")


def upgrade() -> None:
    op.add_column(
        "llm_call_logs",
        sa.Column("tier_used", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("fallback_reason", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "attempt_index", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_llm_call_logs_tier_used",
        "llm_call_logs",
        "tier_used IS NULL OR tier_used IN " + str(VALID_TIERS),
    )
    op.create_index(
        "ix_llm_call_logs_tier_used",
        "llm_call_logs",
        ["tier_used"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_logs_tier_used", table_name="llm_call_logs")
    op.drop_constraint("ck_llm_call_logs_tier_used", "llm_call_logs", type_="check")
    op.drop_column("llm_call_logs", "attempt_index")
    op.drop_column("llm_call_logs", "fallback_reason")
    op.drop_column("llm_call_logs", "tier_used")
