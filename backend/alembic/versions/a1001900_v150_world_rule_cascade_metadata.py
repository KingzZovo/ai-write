"""v1.5.0 C4-8 — add metadata_json to world_rules for cascade revision storage.

Revision ID: a1001900
Revises: a1001800
Create Date: 2026-04-27

The cascade outline handler (C4-7) writes idempotency rev_keys + cascade hints
into the target entity's JSON content column (Outline.content_json). To let
the character / world_rule handlers (C4-8) follow the same pattern, this
migration adds a single ``metadata_json`` JSON column to ``world_rules``
(Character already has ``profile_json``).

The column defaults to ``{}`` server-side so existing rows remain valid
without backfill.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1001900"
down_revision = "a1001800"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "world_rules",
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("world_rules", "metadata_json")
