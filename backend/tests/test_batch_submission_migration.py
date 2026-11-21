"""Run the batch receipt migration on SQLite without real service access."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/a1001952_generation_batches.py'
    spec = importlib.util.spec_from_file_location('batch_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        projects = sa.Table('projects', sa.MetaData(), sa.Column('id', sa.UUID(), primary_key=True))
        projects.create(db)
        pid = uuid4()
        db.execute(projects.insert().values(id=pid))
        monkeypatch.setattr(module, 'op', Operations(MigrationContext.configure(db)))
        module.upgrade()
        batches = sa.Table('generation_batches', sa.MetaData(), autoload_with=db)
        items = sa.Table('generation_batch_items', sa.MetaData(), autoload_with=db)
        for table, names in ((batches, ('id', 'project_id', 'worker_id')), (items, ('task_id', 'batch_id', 'chapter_id', 'volume_id'))):
            for name in names: table.c[name].type = sa.Uuid()
        yield module, db, projects, batches, items, pid
    engine.dispose()


def test_migration_preserves_batch_items_and_requires_explicit_history_disposal(migrated):
    module, db, projects, batches, items, pid = migrated
    rid = uuid4()
    db.execute(batches.insert().values(id=rid, project_id=pid, request_hash='b' * 64, total_chapters=1, status='completed'))
    complete = {'summary': '交钱与背人之间的动作。' * 10_000}
    db.execute(items.insert().values(task_id=uuid4(), batch_id=rid, chapter_id=uuid4(), volume_id=uuid4(),
        volume_idx=1, chapter_idx=1, position=1, outline_json=complete, status='completed', word_count=2800))
    assert db.scalar(sa.select(items.c.outline_json)) == complete
    with pytest.raises(RuntimeError, match='批量生成请求'): module.downgrade()
    assert sa.inspect(db).get_foreign_keys(items.name)[0]['constrained_columns'] == ['batch_id']
    db.execute(projects.delete().where(projects.c.id == pid))
    assert db.scalar(sa.select(sa.func.count()).select_from(batches)) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(items)) == 0


def test_empty_migration_downgrade_keeps_project(migrated):
    module, db, projects, batches, items, pid = migrated
    assert module.down_revision == 'a1001951'
    assert sa.inspect(db).get_unique_constraints(items.name)[0]['column_names'] == ['batch_id', 'position']
    module.downgrade()
    assert sa.inspect(db).get_table_names() == ['projects']
    assert db.scalar(sa.select(projects.c.id)) == pid
