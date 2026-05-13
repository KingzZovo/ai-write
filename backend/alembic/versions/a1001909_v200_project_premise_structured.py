"""v2.0.0 — PR-C: project.premise_structured (JSON) + core_seed (Text) for anti-homogenization.

Revision ID: a1001909
Revises: a1001908
Create Date: 2026-05-13

Rationale:
- lct2 (流程测试2) demonstrated that a free-form premise field ("写一本 200 万字
  的玄幻小说") gives the LLM no anchor to differentiate from existing books,
  leading to template-flavored output indistinguishable from chixin
  (赤心巡天仿写).
- PR-C ships a *structured* premise that the composer can deterministically
  distil into a 1-2 sentence ``core_seed`` consumed by every prompt as the
  primary anti-homogenization signal.
- Both columns are nullable + back-compat: legacy projects with only ``premise``
  remain valid; new projects can populate either or both fields.
"""

from __future__ import annotations

from alembic import op

revision = "a1001909"
down_revision = "a1001908"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
          ADD COLUMN IF NOT EXISTS premise_structured JSONB,
          ADD COLUMN IF NOT EXISTS core_seed TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
          DROP COLUMN IF EXISTS core_seed,
          DROP COLUMN IF EXISTS premise_structured;
        """
    )
