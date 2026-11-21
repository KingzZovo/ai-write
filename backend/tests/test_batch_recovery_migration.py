"""Upgrade existing batch requests without inventing historical save receipts."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def module(name):
    path = Path(__file__).parents[1] / 'alembic/versions' / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def migrated(monkeypatch):
    old, recovery = module('a1001952_generation_batches.py'), module('a1001953_batch_recovery.py')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        sa.Table('projects', sa.MetaData(), sa.Column('id', sa.UUID(), primary_key=True)).create(db)
        ops = Operations(MigrationContext.configure(db))
        monkeypatch.setattr(old, 'op', ops)
        monkeypatch.setattr(recovery, 'op', ops)
        old.upgrade()
        before = sa.Table('generation_batches', sa.MetaData(), autoload_with=db)
        db.execute(before.insert().values(id=uuid4().hex, project_id=uuid4().hex, request_hash='f' * 64,
            status='running', total_chapters=1))
        recovery.upgrade()
        table = sa.Table('generation_batches', sa.MetaData(), autoload_with=db)
        items = sa.Table('generation_batch_items', sa.MetaData(), autoload_with=db)
        yield recovery, db, table, items
    engine.dispose()


def test_old_running_rows_stay_unknown_and_additive_downgrade_keeps_original_request(migrated):
    recovery, db, table, items = migrated
    row = db.execute(sa.select(table)).one()
    assert row.status == 'running' and row.execution_version == 0 and row.heartbeat_at is None
    assert row.recovery_count == 0
    assert recovery.down_revision == 'a1001952'
    assert {'attempt_count', 'attempts_json', 'saved_receipt_json'} <= set(items.c.keys())
    recovery.downgrade()
    columns = {col['name'] for col in sa.inspect(db).get_columns('generation_batches')}
    assert 'execution_version' not in columns
    assert db.scalar(sa.select(sa.func.count()).select_from(table)) == 1


def test_downgrade_refuses_to_drop_new_execution_provenance(migrated):
    recovery, db, table, items = migrated
    db.execute(table.update().values(execution_version=1))
    with pytest.raises(RuntimeError, match='保存证明'):
        recovery.downgrade()
    assert 'saved_receipt_json' in {col['name'] for col in sa.inspect(db).get_columns(items.name)}
