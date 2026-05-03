"""v2.0 (PR-NEO2): faction_events + faction_oppositions tables

Neo4j is the source of truth for faction-level dynamics:
  (:FactionEvent {project_id, kind, chapter, summary})
  (:Organization)-[:INVOLVED_IN]->(:FactionEvent)
  (:Organization)-[:OPPOSED_BY {chapter_start, chapter_end}]->(:Organization)

Postgres mirrors them so ContextPack and the future critic check
"faction state regression" can read without Neo4j round-trips.

Uniqueness:
- faction_events: (project_id, kind, chapter, summary) prevents dup rows
  on re-extraction of the same chapter.
- faction_event_orgs: (event_id, organization_id) prevents dup links.
- faction_oppositions: (project_id, source_org_id, target_org_id,
  chapter_start) lets us version conflicts/treaties over time.

Idempotent CREATE TABLE IF NOT EXISTS pattern (matches
a1001907_v190_organizations_table).
"""

from alembic import op


revision = "a1001910"
down_revision = "a1001909"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS faction_events (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          kind varchar(20) NOT NULL,
          chapter integer NOT NULL,
          summary text DEFAULT '',
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_faction_events_key'
          ) THEN
            ALTER TABLE faction_events
              ADD CONSTRAINT uq_faction_events_key
              UNIQUE (project_id, kind, chapter, summary);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_faction_events_project_chapter "
        "ON faction_events (project_id, chapter);"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS faction_event_orgs (
          id uuid PRIMARY KEY,
          event_id uuid NOT NULL REFERENCES faction_events(id) ON DELETE CASCADE,
          organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_faction_event_orgs_key'
          ) THEN
            ALTER TABLE faction_event_orgs
              ADD CONSTRAINT uq_faction_event_orgs_key
              UNIQUE (event_id, organization_id);
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS faction_oppositions (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          source_org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          target_org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          chapter_start integer NOT NULL,
          chapter_end integer,
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_faction_oppositions_key'
          ) THEN
            ALTER TABLE faction_oppositions
              ADD CONSTRAINT uq_faction_oppositions_key
              UNIQUE (project_id, source_org_id, target_org_id, chapter_start);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS faction_oppositions "
        "DROP CONSTRAINT IF EXISTS uq_faction_oppositions_key;"
    )
    op.execute("DROP TABLE IF EXISTS faction_oppositions;")
    op.execute(
        "ALTER TABLE IF EXISTS faction_event_orgs "
        "DROP CONSTRAINT IF EXISTS uq_faction_event_orgs_key;"
    )
    op.execute("DROP TABLE IF EXISTS faction_event_orgs;")
    op.execute("DROP INDEX IF EXISTS ix_faction_events_project_chapter;")
    op.execute(
        "ALTER TABLE IF EXISTS faction_events "
        "DROP CONSTRAINT IF EXISTS uq_faction_events_key;"
    )
    op.execute("DROP TABLE IF EXISTS faction_events;")
