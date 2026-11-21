from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select, update, create_engine, inspect
from app.models.project import ForeshadowReviewJob as Job
from app.services.foreshadow_review_jobs import enqueue_review_job
from tests.test_foreshadow_review_store import database


@pytest.mark.asyncio
async def test_revision_handoff_deduplicates_without_restarting_completed_work(database):
    session, db, review, _ = database
    Job.__table__.create(session.get_bind())
    source = NS(project_id=review.project_id, chapter_id=review.chapter_id,
                source_hash=review.source_sha256, receipt={'chapter_id': review.chapter_id})
    with session.begin(): await enqueue_review_job(db, source, 100)
    session.execute(update(Job).values(status='completed', attempts=2))
    session.commit()
    with session.begin(): await enqueue_review_job(db, source, 100)
    row = session.execute(select(Job.status, Job.attempts)).one()
    assert row == ('completed', 2)
    session.commit()
    with session.begin(): await enqueue_review_job(db, source, 101)
    assert session.execute(select(Job.status).order_by(Job.source_revision)).scalars().all() == ['completed', 'pending']


@pytest.mark.asyncio
async def test_failed_handoff_transaction_leaves_no_review_job(database):
    session, db, review, _ = database
    Job.__table__.create(session.get_bind())
    source = NS(project_id=review.project_id, chapter_id=review.chapter_id,
                source_hash=review.source_sha256, receipt={})
    with pytest.raises(RuntimeError):
        with session.begin():
            await enqueue_review_job(db, source, 100)
            raise RuntimeError('source acknowledgement failed')
    assert session.execute(select(Job)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_outbox_orders_review_insert_before_ack_and_does_not_commit_failure(monkeypatch, failure):
    from app.services import entity_outbox, foreshadow_review_jobs
    pid, cid = '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222'
    source = NS(project_id=pid, chapter_id=cid, source_hash='a' * 64, receipt={})
    statements = []
    async def execute(statement):
        statements.append(statement)
        if failure and getattr(getattr(statement, 'table', None), 'name', None) == 'foreshadow_review_jobs':
            raise RuntimeError('review enqueue unavailable')
        return NS(all=lambda: [NS(chapter_id=UUID(cid), revision=100)])
    db = NS(execute=execute, scalar=AsyncMock(return_value=pid), commit=AsyncMock())
    @asynccontextmanager
    async def sessions(): yield db
    monkeypatch.setattr(entity_outbox, 'resolve_entity_source', AsyncMock(return_value=source))
    monkeypatch.setattr('app.services.cognition_extraction.enqueue_cognition', AsyncMock())
    if failure:
        with pytest.raises(RuntimeError): await entity_outbox.drain_entity_source_outbox(sessions)
        db.commit.assert_not_called()
        assert not any(str(s).startswith('DELETE') for s in statements)
    else:
        result = await entity_outbox.drain_entity_source_outbox(sessions)
        assert result['transferred'] == 1
        insert_pos = next(i for i, s in enumerate(statements) if getattr(getattr(s, 'table', None), 'name', None) == 'foreshadow_review_jobs')
        delete_pos = next(i for i, s in enumerate(statements) if str(s).startswith('DELETE'))
        assert insert_pos < delete_pos
        db.commit.assert_awaited_once()


def test_review_job_migration_matches_model_and_downgrades():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / 'alembic/versions/a1001937_foreshadow_review_jobs.py'
    spec = importlib.util.spec_from_file_location('review_job_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == 'a1001936'
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = inspect(connection)
            assert {c['name'] for c in inspector.get_columns(Job.__tablename__)} == set(Job.__table__.columns.keys()) - {'discovery_count', 'rescan_discovery', 'discovery_run_id'}
            assert inspector.get_unique_constraints(Job.__tablename__)[0]['column_names'] == ['chapter_id', 'source_revision']
            migration.downgrade()
            assert Job.__tablename__ not in inspect(connection).get_table_names()
    engine.dispose()
