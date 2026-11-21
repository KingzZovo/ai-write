"""Offline DDL/rollback safeguards, not a claim that production PG was migrated."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.arc_planning import ArcPlanningRequest
from app.models.project import Project


@pytest.mark.parametrize("retain", [False, True])
def test_migration_matches_model_and_refuses_to_discard_original_requests(monkeypatch, retain):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/a1001958_arc_planning_requests.py'
    spec = importlib.util.spec_from_file_location('arc_planning_migration_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.down_revision == 'a1001957'
    engine = sa.create_engine('sqlite://')
    Project.__table__.create(engine)
    with engine.begin() as db:
        monkeypatch.setattr(module, 'op', Operations(MigrationContext.configure(db)))
        module.upgrade()
        names = {row['name'] for row in sa.inspect(db).get_columns('arc_planning_requests')}
        assert names == set(ArcPlanningRequest.__table__.columns.keys())
        if retain:
            pid, rid = uuid4(), uuid4()
            db.execute(sa.insert(Project).values(id=pid, title='迁移历史测试'))
            db.execute(sa.insert(ArcPlanningRequest).values(id=rid, project_id=pid, request_json={}, request_hash='a'*64,
                source_json={}, source_hash='b'*64, journal_json={}, journal_hash='c'*64,
                target_chapters=4, target_volume_idx=1, execution_history_json=[]))
            with pytest.raises(RuntimeError, match='原记录'):
                module.downgrade()
            assert db.scalar(sa.select(ArcPlanningRequest.id)) == rid
        else:
            module.downgrade()
            assert not sa.inspect(db).has_table('arc_planning_requests')
    engine.dispose()
