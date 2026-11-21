"""Run the arc history migration against SQLite, including retention rules."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/a1001957_arc_progress_writes.py"
    spec = importlib.util.spec_from_file_location("arc_progress_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        projects = sa.Table("projects", sa.MetaData(), sa.Column("id", sa.UUID(), primary_key=True))
        projects.create(db)
        pid = uuid4()
        db.execute(projects.insert().values(id=pid))
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(db)))
        module.upgrade()
        table = sa.Table("arc_progress_writes", sa.MetaData(), autoload_with=db)
        for key in ("id", "project_id", "volume_id", "chapter_id"): table.c[key].type = sa.Uuid()
        yield module, db, table, projects, pid
    engine.dispose()


def test_migration_preserves_full_history_and_cleans_only_permanently_deleted_projects(migrated):
    module, db, table, projects, pid = migrated
    full = {"content_text": "原保存稿全文。" * 10000}
    db.execute(table.insert().values(id=uuid4(), project_id=pid, volume_id=uuid4(), sequence=1,
        action="acknowledge", chapter_id=uuid4(), manuscript_hash="a"*64, request_hash="b"*64,
        before_json={"legacy": "旧进度"}, manuscript_json=full, result_json={"status": "saved"}, result_hash="c"*64))
    assert db.scalar(sa.select(table.c.manuscript_json)) == full
    assert [key["constrained_columns"] for key in sa.inspect(db).get_foreign_keys(table.name)] == [["project_id"]]
    with pytest.raises(RuntimeError, match="保存稿确认历史"): module.downgrade()
    db.execute(projects.delete().where(projects.c.id == pid))
    assert db.scalar(sa.select(sa.func.count()).select_from(table)) == 0


def test_empty_downgrade_and_unique_sequence(migrated):
    module, db, table, projects, pid = migrated
    assert module.down_revision == "a1001956"
    assert sa.inspect(db).get_unique_constraints(table.name)[0]["column_names"] == ["project_id", "volume_id", "sequence"]
    assert sa.inspect(db).get_indexes(table.name)[0]["column_names"] == ["project_id", "volume_id", "sequence"]
    module.downgrade()
    assert table.name not in sa.inspect(db).get_table_names() and db.scalar(sa.select(projects.c.id)) == pid
