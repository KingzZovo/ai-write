"""Exercise the real additive receipt migration and its deletion contract."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from app.models.project import Chapter, Project, Volume
from app.models.generation_run import GenerationRun


@pytest.fixture
def migrated(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/a1001954_generation_run_submissions.py'
    spec = importlib.util.spec_from_file_location('run_submission_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        for model in (Project, Volume, Chapter, GenerationRun): model.__table__.create(db)
        pid, vid, cid, rid = uuid4(), uuid4(), uuid4(), uuid4()
        db.execute(Project.__table__.insert().values(id=pid, title='迁移前作品'))
        db.execute(Volume.__table__.insert().values(id=vid, project_id=pid, title='原卷', volume_idx=1))
        db.execute(Chapter.__table__.insert().values(id=cid, volume_id=vid, title='原章', chapter_idx=1, global_idx=1))
        db.execute(GenerationRun.__table__.insert().values(id=rid, project_id=pid, chapter_id=cid,
            status='running', phase='critic', checkpoint_data={'drafting': {'text': '迁移前保存的候选。'}}))
        before = dict(db.execute(sa.select(GenerationRun.__table__)).mappings().one())
        monkeypatch.setattr(module, 'op', Operations(MigrationContext.configure(db)))
        module.upgrade()
        receipts = sa.Table('generation_run_submissions', sa.MetaData(), autoload_with=db)
        for name in ('id', 'project_id', 'chapter_id'): receipts.c[name].type = sa.Uuid()
        yield NS(module=module, db=db, receipts=receipts, pid=pid, cid=cid, rid=rid, before=before)
    engine.dispose()


def insert_receipt(r, **changes):
    r.db.execute(r.receipts.insert().values(**{
        'id': r.rid, 'project_id': r.pid, 'chapter_id': r.cid, 'version': 1,
        'request_hash': 'f' * 64, 'max_rewrite_count': 2,
        'source_json': {'chapter': '迁移合同验证'}, 'created_at': datetime.now(timezone.utc), **changes,
    }))


def test_upgrade_does_not_invent_sources_for_old_runs_and_empty_downgrade_preserves_them(migrated):
    r = migrated
    assert r.module.down_revision == 'a1001953'
    assert r.db.scalar(sa.select(sa.func.count()).select_from(r.receipts)) == 0
    assert dict(r.db.execute(sa.select(GenerationRun.__table__)).mappings().one()) == r.before
    r.module.downgrade()
    assert not sa.inspect(r.db).has_table(r.receipts.name)
    assert dict(r.db.execute(sa.select(GenerationRun.__table__)).mappings().one()) == r.before


def test_receipt_survives_chapter_cascade_and_only_book_purge_cleans_it(migrated):
    r = migrated
    insert_receipt(r)
    original = dict(r.db.execute(sa.select(r.receipts)).mappings().one())
    r.db.execute(sa.delete(Chapter).where(Chapter.id == r.cid))
    assert r.db.scalar(sa.select(sa.func.count()).select_from(GenerationRun)) == 0
    assert dict(r.db.execute(sa.select(r.receipts)).mappings().one()) == original
    with pytest.raises(RuntimeError, match='固定请求回执'): r.module.downgrade()
    assert dict(r.db.execute(sa.select(r.receipts)).mappings().one()) == original
    r.db.execute(sa.delete(Project).where(Project.id == r.pid))
    assert r.db.scalar(sa.select(sa.func.count()).select_from(r.receipts)) == 0
    r.module.downgrade()


def test_global_request_identity_cannot_be_reused_for_another_book(migrated):
    r = migrated
    insert_receipt(r)
    other = uuid4()
    r.db.execute(sa.insert(Project).values(id=other, title='另一部作品'))
    with pytest.raises(sa.exc.IntegrityError, match='UNIQUE'):
        insert_receipt(r, project_id=other, chapter_id=uuid4())
    assert r.db.scalar(sa.select(r.receipts.c.project_id)) == r.pid
    assert len(sa.inspect(r.db).get_foreign_keys(r.receipts.name)) == 1
    foreign = sa.inspect(r.db).get_foreign_keys(r.receipts.name)[0]
    assert foreign['constrained_columns'] == ['project_id'] and foreign['options']['ondelete'] == 'CASCADE'
