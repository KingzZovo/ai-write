"""Capture completed chapter source changes in the author transaction."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001925'
down_revision = 'a1001924'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SEQUENCE entity_source_outbox_revisions')
    op.create_table('entity_source_outbox',
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('chapters.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('revision', sa.BigInteger(), nullable=False, server_default=sa.text("nextval('entity_source_outbox_revisions')")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_entity_source_outbox_pending', 'entity_source_outbox', ['updated_at', 'chapter_id'])
    op.execute("""
    CREATE FUNCTION capture_chapter_entity_source() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF ROW(NEW.content_text, NEW.title, NEW.status, NEW.volume_id, NEW.chapter_idx, NEW.global_idx)
               IS NOT DISTINCT FROM
               ROW(OLD.content_text, OLD.title, OLD.status, OLD.volume_id, OLD.chapter_idx, OLD.global_idx) THEN
                RETURN NEW;
            END IF;
        END IF;
        IF NEW.status = 'completed' AND length(trim(coalesce(NEW.content_text, ''))) > 0 THEN
            INSERT INTO entity_source_outbox(chapter_id) VALUES (NEW.id)
            ON CONFLICT (chapter_id) DO UPDATE
            SET revision = nextval('entity_source_outbox_revisions'), updated_at = now();
        END IF;
        RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER chapter_entity_source_changed AFTER INSERT OR UPDATE ON chapters
    FOR EACH ROW EXECUTE FUNCTION capture_chapter_entity_source()
    """)
    op.execute("""
    CREATE FUNCTION capture_volume_entity_sources() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF ROW(NEW.project_id, NEW.volume_idx) IS DISTINCT FROM ROW(OLD.project_id, OLD.volume_idx) THEN
            INSERT INTO entity_source_outbox(chapter_id)
            SELECT id FROM chapters WHERE volume_id = NEW.id AND status = 'completed'
                AND length(trim(coalesce(content_text, ''))) > 0 ORDER BY id
            ON CONFLICT (chapter_id) DO UPDATE
            SET revision = nextval('entity_source_outbox_revisions'), updated_at = now();
        END IF;
        RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER volume_entity_sources_changed AFTER UPDATE ON volumes
    FOR EACH ROW EXECUTE FUNCTION capture_volume_entity_sources()
    """)
    # Existing books need a deliberate rebuild; do not silently enqueue paid LLM calls.


def downgrade():
    op.execute('DROP TRIGGER volume_entity_sources_changed ON volumes')
    op.execute('DROP FUNCTION capture_volume_entity_sources()')
    op.execute('DROP TRIGGER chapter_entity_source_changed ON chapters')
    op.execute('DROP FUNCTION capture_chapter_entity_source()')
    op.drop_table('entity_source_outbox')
    op.execute('DROP SEQUENCE entity_source_outbox_revisions')
