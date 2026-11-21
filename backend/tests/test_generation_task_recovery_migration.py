"""Keep historical output and execution proof; never synthesize recovery sources."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/a1001956_generation_task_recovery.py'
    spec = importlib.util.spec_from_file_location('task_recovery_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    try:
        with engine.begin() as db:
            old = sa.Table('generation_tasks', sa.MetaData(), sa.Column('id', sa.String(), primary_key=True),
                sa.Column('status', sa.String()), sa.Column('params_json', sa.JSON()),
                sa.Column('progress_text', sa.Text()), sa.Column('result_text', sa.Text()),
                sa.Column('execution_id', sa.String()), sa.Column('execution_params_json', sa.JSON()),
                sa.Column('created_at', sa.DateTime(timezone=True)))
            old.create(db)
            for status in ('pending', 'running', 'evaluating', 'completed', 'cancelled'):
                db.execute(old.insert().values(id=str(uuid4()), status=status, params_json={'request': status},
                    progress_text='原进度。', result_text='原候选。', execution_id=str(uuid4()),
                    execution_params_json={'original': status}, created_at=datetime.now(timezone.utc)))
            before = list(db.execute(sa.select(old).order_by(old.c.id)).mappings())
            monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(db)))
            migration.upgrade()
            current = sa.Table('generation_tasks', sa.MetaData(), autoload_with=db)
            yield migration, db, current, before
    finally:
        engine.dispose()


def test_upgrade_preserves_old_rows_without_guessing_progress_or_source(migrated):
    migration, db, table, before = migrated
    assert migration.down_revision == 'a1001955'
    rows = list(db.execute(sa.select(table).order_by(table.c.id)).mappings())
    for row, old in zip(rows, before):
        assert all(row[key] == value for key, value in old.items())
        assert row['execution_source_json'] is None and row['progress_kind'] is None
        assert row['dispatched_at'] is None and row['recovery_history_json'] == []
    assert 'ix_generation_tasks_recovery' in {index['name'] for index in sa.inspect(db).get_indexes(table.name)}
    migration.downgrade()
    downgraded = sa.Table('generation_tasks', sa.MetaData(), autoload_with=db)
    assert list(db.execute(sa.select(downgraded).order_by(downgraded.c.id)).mappings()) == before


@pytest.mark.parametrize('values', [
    {'execution_source_json': {'version': 1}}, {'progress_kind': 'candidate'},
    {'dispatched_at': datetime.now(timezone.utc)}, {'recovery_history_json': [{'outcome': 'needs_review'}]},
    {'recovery_history_json': {'legacy': 'unverified'}},
])
def test_used_or_uncertain_recovery_data_blocks_destructive_downgrade(migrated, values):
    migration, db, table, before = migrated
    db.execute(table.update().where(table.c.id == before[0]['id']).values(**values))
    expected = list(db.execute(sa.select(table).order_by(table.c.id)).mappings())
    with pytest.raises(RuntimeError, match='恢复来源或派发历史'): migration.downgrade()
    assert 'execution_source_json' in {column['name'] for column in sa.inspect(db).get_columns(table.name)}
    assert list(db.execute(sa.select(table).order_by(table.c.id)).mappings()) == expected
