"""v1.9 — enforce unique character name per project.

Revision ID: a1001901
Revises: a1001900
Create Date: 2026-04-30

Rationale:
- We materialize entity snapshots (Neo4j) into Postgres characters.
- Idempotency requires a stable unique key. The natural key is
  (project_id, name).

This constraint is intentionally minimal; profile_json merge semantics are
handled at the application layer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1001901"
down_revision = "a1001900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_characters_project_id_name",
        "characters",
        ["project_id", "name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_characters_project_id_name",
        "characters",
        type_="unique",
    )
