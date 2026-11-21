"""Restore the chapter content_hash trigger and backfill existing bodies.

`a1001949` created `maintain_chapter_content_hash()` + the
`chapter_content_hash_changed` trigger. On databases where a later revision
rebuilt `chapters`, the column survives but the function and trigger do not —
实测(head=a1001961):1096 章有正文、`content_hash` 全为空、`chapters` 上零触发器。

That breaks every reader that uses "hash 非空" as the cheap signal for
"这一章已落库正文" (cognition boundary, arc progress, 前文衔接), so downstream
chapters report "前文没有正文" and the whole pipeline stalls.

This revision is idempotent: it recreates the function/trigger when missing and
backfills hashes for rows that already have a body.
"""
from alembic import op

revision = 'a1001962'
down_revision = 'a1001961'
branch_labels = None
depends_on = None


FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION maintain_chapter_content_hash() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.content_text COLLATE "C" IS NOT DISTINCT FROM OLD.content_text COLLATE "C"
           AND OLD.content_hash IS NOT NULL THEN
            NEW.content_hash := OLD.content_hash;
            RETURN NEW;
        END IF;
    END IF;
    NEW.content_hash := encode(sha256(convert_to(coalesce(NEW.content_text, ''), 'UTF8')), 'hex');
    RETURN NEW;
END $$
"""


def upgrade():
    op.execute(FUNCTION_SQL)
    # Recreate the trigger unconditionally: DROP IF EXISTS keeps this safe on
    # databases that still have it from a1001949.
    op.execute("DROP TRIGGER IF EXISTS chapter_content_hash_changed ON chapters")
    op.execute(
        """
        CREATE TRIGGER chapter_content_hash_changed
        BEFORE INSERT OR UPDATE OF content_text, content_hash ON chapters
        FOR EACH ROW EXECUTE FUNCTION maintain_chapter_content_hash()
        """
    )
    # Backfill without touching content, summaries, statuses or timestamps.
    # Only rows that already have a body but no hash, so authors' accepted
    # hashes are never rewritten.
    op.execute(
        """
        UPDATE chapters
           SET content_hash = encode(sha256(convert_to(coalesce(content_text, ''), 'UTF8')), 'hex')
         WHERE coalesce(content_hash, '') = ''
           AND coalesce(content_text, '') <> ''
        """
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS chapter_content_hash_changed ON chapters")
    op.execute("DROP FUNCTION IF EXISTS maintain_chapter_content_hash()")