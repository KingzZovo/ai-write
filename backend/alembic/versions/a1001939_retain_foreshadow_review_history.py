"""Keep project-owned evidence when chapters, volumes or foreshadows disappear."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001939'
down_revision = 'a1001938'
branch_labels = None
depends_on = None

NAMING = {'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'}
SOURCES = {'chapter_id': 'chapters', 'foreshadow_id': 'foreshadows'}


def upgrade():
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys('foreshadow_reviews')
    names = []
    for column, table in SOURCES.items():
        matches = [fk for fk in foreign_keys if fk['constrained_columns'] == [column]
                   and fk['referred_table'] == table and fk['referred_columns'] == ['id']]
        if len(matches) != 1:
            raise RuntimeError('Unexpected foreshadow review source constraints; inspect schema before migration')
        names.append(matches[0]['name'] or f'fk_foreshadow_reviews_{column}_{table}')
    # On PostgreSQL this uses ALTER TABLE; SQLite uses a table copy for tests.
    # The project FK and source UUID columns remain intact.
    with op.batch_alter_table('foreshadow_reviews', naming_convention=NAMING) as batch:
        for name in names:
            batch.drop_constraint(name, type_='foreignkey')


def downgrade():
    # Never delete retained history to make an older FK contract fit.
    missing = op.get_bind().execute(sa.text('''
        SELECT 1 FROM foreshadow_reviews r
        LEFT JOIN chapters c ON c.id = r.chapter_id
        LEFT JOIN foreshadows f ON f.id = r.foreshadow_id
        WHERE c.id IS NULL OR f.id IS NULL LIMIT 1
    ''')).first()
    if missing is not None:
        raise RuntimeError('Cannot restore cascading source constraints while retained review history has missing sources')
    with op.batch_alter_table('foreshadow_reviews', naming_convention=NAMING) as batch:
        for column, table in SOURCES.items():
            batch.create_foreign_key(f'fk_foreshadow_reviews_{column}_{table}', table,
                                     [column], ['id'], ondelete='CASCADE')
