"""Durable invalidations survive chapter/volume/project deletion."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a1001926'
down_revision = 'a1001925'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SEQUENCE entity_invalidation_revisions')
    op.create_table('entity_invalidations',
        sa.Column('project_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('chapter_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('revision', sa.BigInteger(), nullable=False, server_default=sa.text("nextval('entity_invalidation_revisions')")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_error', sa.String(128)),
    )
    op.create_index('ix_entity_invalidations_pending', 'entity_invalidations', ['updated_at'])
    op.add_column('character_memory_cards', sa.Column('source_chapter_id', postgresql.UUID(as_uuid=True)))
    op.add_column('character_memory_cards', sa.Column('source_hash', sa.String(64)))
    op.create_index('ix_character_memory_cards_source', 'character_memory_cards', ['project_id', 'source_chapter_id'])
    op.execute("""
    CREATE FUNCTION invalidate_chapter_entity_source() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE pid uuid;
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF ROW(NEW.content_text,NEW.title,NEW.status,NEW.volume_id,NEW.chapter_idx,NEW.global_idx)
               IS NOT DISTINCT FROM ROW(OLD.content_text,OLD.title,OLD.status,OLD.volume_id,OLD.chapter_idx,OLD.global_idx) THEN
                RETURN NEW;
            END IF;
        END IF;
        SELECT project_id INTO pid FROM volumes WHERE id=OLD.volume_id;
        IF pid IS NOT NULL THEN
            INSERT INTO entity_invalidations(project_id,chapter_id) VALUES(pid,OLD.id)
            ON CONFLICT(project_id,chapter_id) DO UPDATE
                SET revision=nextval('entity_invalidation_revisions'),updated_at=now(),last_error=NULL;
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER chapter_entity_source_invalidated BEFORE UPDATE OR DELETE ON chapters
    FOR EACH ROW EXECUTE FUNCTION invalidate_chapter_entity_source()
    """)
    op.execute("""
    CREATE FUNCTION invalidate_volume_entity_sources() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' THEN
            IF ROW(NEW.project_id,NEW.volume_idx) IS NOT DISTINCT FROM ROW(OLD.project_id,OLD.volume_idx) THEN
                RETURN NEW;
            END IF;
        END IF;
        INSERT INTO entity_invalidations(project_id,chapter_id)
            SELECT OLD.project_id,id FROM chapters WHERE volume_id=OLD.id ORDER BY id
        ON CONFLICT(project_id,chapter_id) DO UPDATE
            SET revision=nextval('entity_invalidation_revisions'),updated_at=now(),last_error=NULL;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """)
    op.execute("""
    CREATE TRIGGER volume_entity_sources_invalidated BEFORE UPDATE OR DELETE ON volumes
    FOR EACH ROW EXECUTE FUNCTION invalidate_volume_entity_sources()
    """)


def downgrade():
    op.execute('DROP TRIGGER volume_entity_sources_invalidated ON volumes')
    op.execute('DROP FUNCTION invalidate_volume_entity_sources()')
    op.execute('DROP TRIGGER chapter_entity_source_invalidated ON chapters')
    op.execute('DROP FUNCTION invalidate_chapter_entity_source()')
    op.drop_index('ix_character_memory_cards_source', table_name='character_memory_cards')
    op.drop_column('character_memory_cards', 'source_hash')
    op.drop_column('character_memory_cards', 'source_chapter_id')
    op.drop_table('entity_invalidations')
    op.execute('DROP SEQUENCE entity_invalidation_revisions')
