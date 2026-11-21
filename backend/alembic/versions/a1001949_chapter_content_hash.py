"""Fingerprint saved chapter bodies once per content write, not on every read."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001949'
down_revision = 'a1001948'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chapters', sa.Column('content_hash', sa.String(64), nullable=True))
    op.execute("""
    CREATE FUNCTION maintain_chapter_content_hash() RETURNS trigger LANGUAGE plpgsql AS $$
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
    """)
    # A client cannot supply a stale/forged hash. Summary/order-only writes
    # preserve the old hash without reading and hashing the body again.
    op.execute("""
    CREATE TRIGGER chapter_content_hash_changed BEFORE INSERT OR UPDATE OF content_text, content_hash ON chapters
    FOR EACH ROW EXECUTE FUNCTION maintain_chapter_content_hash()
    """)
    # One source read per existing row, inside the same migration transaction.
    # Do not edit content, receipts, author decisions, summaries or timestamps.
    op.execute("UPDATE chapters SET content_hash = NULL")


def downgrade():
    op.execute('DROP TRIGGER chapter_content_hash_changed ON chapters')
    op.execute('DROP FUNCTION maintain_chapter_content_hash()')
    op.drop_column('chapters', 'content_hash')
