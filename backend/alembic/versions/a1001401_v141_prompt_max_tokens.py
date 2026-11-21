"""v1.4.1 — raise default prompt_assets.max_tokens

Revision ID: a1001401
Revises: a1001400
Create Date: 2026-04-24

Changes:
- prompt_assets: bump column default 4096 -> 8192 so new prompts get a
  larger output budget out of the box.
- Data backfill:
    * task_type LIKE 'outline%' and current max_tokens=4096 -> 16384
      (book/volume/chapter outlines routinely exceed 10k Chinese chars).
    * all other rows with current max_tokens=4096 -> 8192.
    * rows with user-customized values (!= 4096) are left untouched.

Downgrade reverses: server_default back to 4096; rows at 8192 -> 4096
(outline* at 16384 -> 4096). Any rows the user modified after the upgrade
will only be reverted if they still hold the post-upgrade defaults.
"""
from alembic import op

revision = "a1001401"
down_revision = "a1001400"
branch_labels = None
depends_on = None


def upgrade():
    # --- Column default (applies to future INSERTs with no explicit value)
    op.alter_column(
        "prompt_assets",
        "max_tokens",
        server_default="8192",
    )

    # --- Data backfill: outline prompts need bigger budgets than the rest.
    op.execute(
        """
        UPDATE prompt_assets
        SET max_tokens = 16384
        WHERE max_tokens = 4096
          AND task_type LIKE 'outline%'
        """
    )
    op.execute(
        """
        UPDATE prompt_assets
        SET max_tokens = 8192
        WHERE max_tokens = 4096
        """
    )


def downgrade():
    # Revert outline* first so we don't accidentally drop them to 8192.
    op.execute(
        """
        UPDATE prompt_assets
        SET max_tokens = 4096
        WHERE max_tokens = 16384
          AND task_type LIKE 'outline%'
        """
    )
    op.execute(
        """
        UPDATE prompt_assets
        SET max_tokens = 4096
        WHERE max_tokens = 8192
        """
    )
    op.alter_column(
        "prompt_assets",
        "max_tokens",
        server_default="4096",
    )
