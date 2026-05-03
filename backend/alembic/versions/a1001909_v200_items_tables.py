"""v2.0 (PR-NEO1): items + item_events tables for Neo4j -> PG materialization

Neo4j is the source of truth for extracted items and their
ownership / use / transfer events. Postgres is the read-side projection,
used by:
- ContextPack 「当前道具持有」块 (PR-NEO4)
- consistency:item_missing checker (already reads `items` rows; PR-NEO1
  starts populating them)
- front-end item card (future)

Uniqueness:
- items: (project_id, name)
- item_events: (project_id, item_id, chapter_idx, kind, actor_name, target_name)

Idempotent: safe to re-apply on dev environments that may already have the
tables (mirrors the a1001907_v190_organizations_table pattern).
"""

from alembic import op


revision = "a1001909"
down_revision = "a1001908"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          name varchar(200) NOT NULL,
          kind varchar(50) DEFAULT '',
          first_owner varchar(200) DEFAULT '',
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_items_project_name'
          ) THEN
            ALTER TABLE items
              ADD CONSTRAINT uq_items_project_name
              UNIQUE (project_id, name);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS item_events (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          item_id uuid NOT NULL REFERENCES items(id) ON DELETE CASCADE,
          chapter_idx integer NOT NULL,
          kind varchar(20) NOT NULL,
          actor_name varchar(200) DEFAULT '',
          target_name varchar(200) DEFAULT '',
          note text DEFAULT '',
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_item_events_key'
          ) THEN
            ALTER TABLE item_events
              ADD CONSTRAINT uq_item_events_key
              UNIQUE (project_id, item_id, chapter_idx, kind, actor_name, target_name);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_events_project_chapter "
        "ON item_events (project_id, chapter_idx);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_item_events_project_chapter;")
    op.execute(
        "ALTER TABLE IF EXISTS item_events "
        "DROP CONSTRAINT IF EXISTS uq_item_events_key;"
    )
    op.execute("DROP TABLE IF EXISTS item_events;")
    op.execute(
        "ALTER TABLE IF EXISTS items "
        "DROP CONSTRAINT IF EXISTS uq_items_project_name;"
    )
    op.execute("DROP TABLE IF EXISTS items;")
