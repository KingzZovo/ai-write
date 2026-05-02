"""v1.5.0 C1 hotfix — backfill scene_planner / scene_writer endpoint_id.

Revision ID: a1001601
Revises: a1001600
Create Date: 2026-04-27

The a1001600 seed inserted scene_planner / scene_writer rows without
an endpoint_id. PromptRegistry.resolve(task_type) cannot resolve a
route when endpoint_id is NULL, raising:
    Prompt '场景写作（C1）' (task scene_writer) has no endpoint configured.

Fix: backfill endpoint_id from a tier-matching sibling row.
  - scene_writer  (flagship) -> endpoint of 'generation'
  - scene_planner (standard) -> endpoint of 'outline_chapter'

Idempotent: only updates rows where endpoint_id IS NULL, and only when
the sibling task_type already has an endpoint assigned.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1001601"
down_revision = "a1001600"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE prompt_assets
        SET endpoint_id = (
            SELECT endpoint_id
            FROM prompt_assets
            WHERE task_type = 'generation' AND endpoint_id IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
        ),
        updated_at = NOW()
        WHERE task_type = 'scene_writer'
          AND endpoint_id IS NULL
          AND EXISTS (
              SELECT 1 FROM prompt_assets
              WHERE task_type = 'generation' AND endpoint_id IS NOT NULL
          );
        """
    )
    op.execute(
        """
        UPDATE prompt_assets
        SET endpoint_id = (
            SELECT endpoint_id
            FROM prompt_assets
            WHERE task_type = 'outline_chapter' AND endpoint_id IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
        ),
        updated_at = NOW()
        WHERE task_type = 'scene_planner'
          AND endpoint_id IS NULL
          AND EXISTS (
              SELECT 1 FROM prompt_assets
              WHERE task_type = 'outline_chapter' AND endpoint_id IS NOT NULL
          );
        """
    )


def downgrade() -> None:
    # Pure data backfill; safe to no-op on downgrade. Clearing endpoint_id
    # would re-introduce the original bug, so we leave the values in place.
    pass
