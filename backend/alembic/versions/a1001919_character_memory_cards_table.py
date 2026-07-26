"""Tier-2 memory pyramid: character_memory_cards table.

One row per (project, character, book-global chapter, card_type). Short
verbatim excerpts (<=300 chars) cut deterministically from chapter full text
by entity extraction — no extra LLM calls. card_type is 'first_appearance'
(the character's first-seen chapter) or 'key_moment' (occurrence nearest the
chapter midpoint). Re-extraction upserts on the unique key; retention keeps
at most MEMORY_CARDS_PER_CHARACTER cards per character.
"""

from alembic import op

revision = "a1001919"
down_revision = "a1001918"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent to support local dev environments (matches a1001918 style).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS character_memory_cards (
          id uuid PRIMARY KEY,
          project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          character_name varchar(128) NOT NULL,
          global_idx integer NOT NULL,
          excerpt text NOT NULL,
          card_type varchar(32) NOT NULL DEFAULT 'key_moment',
          created_at timestamptz
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_character_memory_cards_lookup
          ON character_memory_cards (project_id, character_name, global_idx);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_character_memory_cards_key'
          ) THEN
            ALTER TABLE character_memory_cards
              ADD CONSTRAINT uq_character_memory_cards_key
              UNIQUE (project_id, character_name, global_idx, card_type);
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS character_memory_cards
          DROP CONSTRAINT IF EXISTS uq_character_memory_cards_key;
        DROP INDEX IF EXISTS ix_character_memory_cards_lookup;
        DROP TABLE IF EXISTS character_memory_cards;
        """
    )
