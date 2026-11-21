"""Preservation of author outline history in the actual migration, on SQLite."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/a1001951_volume_outline_writes.py'
    spec = importlib.util.spec_from_file_location('volume_outline_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        metadata = sa.MetaData()
        projects = sa.Table('projects', metadata, sa.Column('id', sa.UUID(), primary_key=True))
        projects.create(db)
        pid, vid = uuid4(), uuid4()
        db.execute(projects.insert().values(id=pid))
        monkeypatch.setattr(module, 'op', Operations(MigrationContext.configure(db)))
        module.upgrade()
        table = sa.Table('volume_outline_writes', sa.MetaData(), autoload_with=db)
        for name in ('id', 'project_id', 'volume_id'): table.c[name].type = sa.Uuid()
        yield module, db, table, projects, pid, vid
    engine.dispose()


def test_migration_keeps_complete_history_and_blocks_destructive_downgrade(migrated):
    module, db, table, projects, pid, vid = migrated
    full = {'raw_text': '原卷纲。' * 10_000}
    db.execute(table.insert().values(id=uuid4(), project_id=pid, volume_id=vid,
        request_hash='a'*64, before_json=full, result_json={'status': 'saved', 'source': full}))
    assert db.execute(sa.select(table.c.before_json)).scalar_one() == full
    with pytest.raises(RuntimeError, match='保存历史'): module.downgrade()
    assert db.scalar(sa.select(sa.func.count()).select_from(table)) == 1
    assert [foreign['constrained_columns'] for foreign in sa.inspect(db).get_foreign_keys(table.name)] == [['project_id']]
    db.execute(projects.delete().where(projects.c.id == pid))
    assert db.scalar(sa.select(sa.func.count()).select_from(table)) == 0


def test_empty_downgrade_preserves_projects(migrated):
    module, db, table, projects, pid, _ = migrated
    assert module.down_revision == 'a1001950'
    assert sa.inspect(db).get_indexes(table.name)[0]['column_names'] == ['project_id', 'volume_id', 'created_at', 'id']
    module.downgrade()
    assert table.name not in sa.inspect(db).get_table_names()
    assert db.scalar(sa.select(projects.c.id)) == pid
