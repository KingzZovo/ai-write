"""Add execution proof without inventing provenance for historical tasks."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/a1001955_generation_task_execution.py'
    spec = importlib.util.spec_from_file_location('task_execution_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    try:
        with engine.begin() as db:
            old = sa.Table('generation_tasks', sa.MetaData(),
                sa.Column('id', sa.String(), primary_key=True),
                sa.Column('status', sa.String()), sa.Column('params_json', sa.JSON()),
                sa.Column('progress_text', sa.Text()), sa.Column('result_text', sa.Text()))
            old.create(db)
            for status in ('pending', 'running', 'completed', 'cancelled'):
                db.execute(old.insert().values(id=str(uuid4()), status=status,
                    params_json={'original_request': status}, progress_text='原进度。', result_text='原候选。'))
            before = list(db.execute(sa.select(old).order_by(old.c.id)).mappings())
            monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(db)))
            migration.upgrade()
            current = sa.Table('generation_tasks', sa.MetaData(), autoload_with=db)
            yield migration, db, current, before
    finally:
        engine.dispose()


def test_historical_statuses_keep_original_output_without_execution_proof(migrated):
    migration, db, current, before = migrated
    assert migration.down_revision == 'a1001954'
    rows = list(db.execute(sa.select(current).order_by(current.c.id)).mappings())
    for row, original in zip(rows, before):
        assert all(row[key] == value for key, value in original.items())
        assert row['execution_version'] == row['attempt_count'] == 0 and row['attempts_json'] == []
        assert all(row[key] is None for key in ('execution_id', 'heartbeat_at', 'execution_params_json', 'saved_manuscript_json'))
    migration.downgrade()
    downgraded = sa.Table('generation_tasks', sa.MetaData(), autoload_with=db)
    assert list(db.execute(sa.select(downgraded).order_by(downgraded.c.id)).mappings()) == before


def test_downgrade_refuses_to_remove_durable_execution_and_delivery(migrated):
    migration, db, table, before = migrated
    db.execute(table.update().where(table.c.id == before[0]['id']).values(
        execution_version=1, execution_id=uuid4().hex, attempt_count=1,
        execution_params_json={'submission': {'fingerprint': 'd' * 64}},
        attempts_json=[{'started_at': '2026-10-09'}], saved_manuscript_json={'body_hash': 'a' * 64}))
    with pytest.raises(RuntimeError, match='保存证明'): migration.downgrade()
    assert 'saved_manuscript_json' in {column['name'] for column in sa.inspect(db).get_columns(table.name)}
    assert db.scalar(sa.select(table.c.attempt_count).where(table.c.id == before[0]['id'])) == 1
