"""Offline migration preservation checks; PostgreSQL concurrency lives separately."""
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text, insert

from app.models.project import (Project, Volume, Chapter, ForeshadowReviewJob as Job, ForeshadowDiscoveryRun as Run,
    ForeshadowDiscovery as Candidate, ForeshadowDiscoveryRunItem as Item, ForeshadowReviewRequest as Receipt)
from tests.test_foreshadow_review_retention import migration


def legacy_records(connection):
    run, project, chapter = uuid4(), uuid4(), uuid4()
    connection.execute(text('''INSERT INTO foreshadow_discovery_runs
        (id, project_id, chapter_id, source_sha256, source_snapshot, candidate_count, created_at)
        VALUES (:id, :project, :chapter, :sha, '{}', 2, '2026-10-08T00:00:00')'''),
        dict(id=run.hex, project=project.hex, chapter=chapter.hex, sha='a' * 64))
    for status in ('accepted', 'dismissed'):
        candidate = uuid4()
        connection.execute(text('''INSERT INTO foreshadow_discoveries
            (id, run_id, description, type, evidence, evidence_start, evidence_end, reason, status,
             foreshadow_id, handled_at, handled_by, created_at)
            VALUES (:id, :run, :description, 'plot', '原句', 3, 5, :reason, :status,
                    :fid, '2026-10-08T00:01:00', '原作者', '2026-10-08T00:00:00')'''),
            dict(id=candidate.hex, run=run.hex, description=f'原描述 {status}',
                reason=f'原说明\n{status}', status=status, fid=candidate.hex if status == 'accepted' else None))
    return run, project, chapter


def read_legacy(connection):
    return (connection.execute(text('SELECT * FROM foreshadow_discovery_runs ORDER BY id')).mappings().all(),
            connection.execute(text('SELECT * FROM foreshadow_discoveries ORDER BY id')).mappings().all())


def test_upgrade_and_representable_downgrade_preserve_original_ids_evidence_and_decisions():
    base, prior, step = (migration(name) for name in (
        'a1001937_foreshadow_review_jobs', 'a1001942_foreshadow_discoveries', 'a1001944_foreshadow_rescans'))
    assert step.down_revision == 'a1001943'
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        for model in (Project, Volume, Chapter): model.__table__.create(connection)
        with Operations.context(MigrationContext.configure(connection)):
            base.upgrade(); prior.upgrade()
            legacy_records(connection)
            before_runs, before_candidates = read_legacy(connection)
            step.upgrade()
            for model in (Job, Run, Candidate, Item, Receipt):
                assert {c['name'] for c in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            after_runs, after_candidates = read_legacy(connection)
            assert after_candidates == before_candidates
            assert [{key: row[key] for key in before_runs[0]} for row in after_runs] == before_runs
            assert after_runs[0]['scan_revision'] == 0 and after_runs[0]['model_name'] is None
            members = connection.execute(text('SELECT * FROM foreshadow_discovery_run_items ORDER BY position')).mappings().all()
            assert [m['candidate_id'] for m in members] == [c['id'] for c in before_candidates]
            assert [m['reason'] for m in members] == [c['reason'] for c in before_candidates]
            assert [m['position'] for m in members] == [0, 1]
            assert inspect(connection).get_unique_constraints(Run.__tablename__)[0]['column_names'] == ['chapter_id', 'source_sha256', 'scan_revision']
            assert {fk['referred_table'] for fk in inspect(connection).get_foreign_keys(Receipt.__tablename__)} == {'projects'}
            step.downgrade()
            assert read_legacy(connection) == (before_runs, before_candidates)
            step.upgrade()
            assert connection.execute(text('SELECT COUNT(*) FROM foreshadow_discovery_run_items')).scalar_one() == 2
    engine.dispose()


@pytest.mark.parametrize('data', ['rescan', 'model_name', 'request', 'rescan_job', 'used_scan'])
def test_downgrade_refuses_to_erase_new_evidence_or_request_identity(data):
    base, prior, step = (migration(name) for name in (
        'a1001937_foreshadow_review_jobs', 'a1001942_foreshadow_discoveries', 'a1001944_foreshadow_rescans'))
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        for model in (Project, Volume, Chapter): model.__table__.create(connection)
        with Operations.context(MigrationContext.configure(connection)):
            base.upgrade(); prior.upgrade(); step.upgrade()
            pid, cid = uuid4(), uuid4()
            if data in {'rescan', 'model_name'}:
                connection.execute(insert(Run).values(id=uuid4(), project_id=pid, chapter_id=cid,
                    source_sha256='a' * 64, source_snapshot={}, candidate_count=0,
                    scan_revision=5 if data == 'rescan' else 0, model_name='test-model' if data == 'model_name' else None))
            elif data == 'request':
                connection.execute(insert(Receipt).values(id=uuid4(), project_id=pid, chapter_id=cid,
                    request_sha256='b' * 64, job_id=uuid4(), rescan_discovery=True))
            else:
                connection.execute(insert(Job).values(id=uuid4(), project_id=pid, chapter_id=cid,
                    source_revision=1, source_hash='b' * 64, source_json={}, rescan_discovery=data == 'rescan_job',
                    discovery_run_id=uuid4() if data == 'used_scan' else None))
            with pytest.raises(RuntimeError, match='Preserve rescan'): step.downgrade()
            assert Item.__tablename__ in inspect(connection).get_table_names()
            assert 'scan_revision' in {c['name'] for c in inspect(connection).get_columns(Run.__tablename__)}
    engine.dispose()
