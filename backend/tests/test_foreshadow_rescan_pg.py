"""Actual PG request serialization and populated migrations in disposable schemas."""
import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, func, text

from app.models.project import Chapter, ForeshadowReviewJob as Job, ForeshadowReviewRequest as Receipt
from app.models.entity_extraction import EntitySourceOutbox, entity_source_outbox_revisions
from app.services.foreshadow_review_jobs import create_review_job
from app.services.foreshadow_review_requests import ReviewRequestConflict
from tests.test_pipeline_control import runtime_db
from tests.test_foreshadow_review_retention import migration

pytestmark = pytest.mark.external_services


def legacy_schema(connection):
    with Operations.context(MigrationContext.configure(connection)):
        migration('a1001937_foreshadow_review_jobs').upgrade()
        migration('a1001942_foreshadow_discoveries').upgrade()


@pytest_asyncio.fixture
async def rescan_db(runtime_db):
    r = runtime_db
    async with r.engine.begin() as connection:
        await connection.run_sync(legacy_schema)
        def finish(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration('a1001944_foreshadow_rescans').upgrade()
            entity_source_outbox_revisions.create(sync, checkfirst=True)
            EntitySourceOutbox.__table__.create(sync)
        await connection.run_sync(finish)
    return r


async def submit(r, request_id, chapter=None, content='原稿。'):
    async with r.sessions() as db:
        result = await create_review_job(db, r.project, chapter or r.chapter, content,
            request_id=request_id, rescan_discovery=True)
        await db.commit()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize('same_request', [True, False])
async def test_concurrent_rescans_create_one_job_and_bind_all_requests(rescan_db, same_request):
    r = rescan_db; first = uuid4(); second = first if same_request else uuid4()
    one, two = await asyncio.wait_for(asyncio.gather(submit(r, first), submit(r, second)), timeout=15)
    assert one['id'] == two['id'] and one['rescan_discovery'] and two['rescan_discovery']
    async with r.sessions() as db:
        assert await db.scalar(select(func.count(Job.id))) == 1
        assert await db.scalar(select(func.count(Receipt.id))) == (1 if same_request else 2)


@pytest.mark.asyncio
async def test_same_request_id_racing_across_chapters_has_one_winner(rescan_db):
    r = rescan_db
    async with r.sessions() as db:
        first = await db.get(Chapter, r.chapter)
        second = Chapter(volume_id=first.volume_id, title='另一章', chapter_idx=2, status='draft', content_text='另一稿。')
        db.add(second); await db.flush(); chapter = second.id; await db.commit()
    request_id = uuid4()
    outcomes = await asyncio.wait_for(asyncio.gather(submit(r, request_id), submit(r, request_id, chapter, '另一稿。'),
        return_exceptions=True), timeout=15)
    assert sum(isinstance(outcome, ReviewRequestConflict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    async with r.sessions() as db:
        assert await db.scalar(select(func.count(Job.id))) == 1
        assert await db.scalar(select(func.count(Receipt.id))) == 1


@pytest.mark.asyncio
async def test_request_receipt_and_job_roll_back_in_one_real_transaction(rescan_db):
    r = rescan_db; request_id = uuid4()
    async with r.sessions() as db:
        await create_review_job(db, r.project, r.chapter, '原稿。', request_id=request_id, rescan_discovery=True)
        await db.rollback()
    async with r.sessions() as db:
        assert await db.scalar(select(func.count(Job.id))) == 0
        assert await db.scalar(select(func.count(Receipt.id))) == 0
    assert (await submit(r, request_id))['status'] == 'pending'


@pytest.mark.asyncio
async def test_pg_upgrade_downgrade_upgrade_preserves_populated_legacy_history(runtime_db):
    r = runtime_db; run_id, candidate_id = uuid4(), uuid4()
    async with r.engine.begin() as connection:
        await connection.run_sync(legacy_schema)
        await connection.execute(text('''INSERT INTO foreshadow_discovery_runs
            (id, project_id, chapter_id, source_sha256, source_snapshot, candidate_count, created_at)
            VALUES (:id, :project, :chapter, :sha, '{}', 1, now())'''),
            dict(id=run_id, project=r.project, chapter=r.chapter, sha='a' * 64))
        await connection.execute(text('''INSERT INTO foreshadow_discoveries
            (id, run_id, description, type, evidence, evidence_start, evidence_end, reason, status, handled_at, handled_by, created_at)
            VALUES (:id, :run, '原描述', 'plot', '原句', 0, 2, '原说明', 'dismissed', now(), '作者', now())'''),
            dict(id=candidate_id, run=run_id))
        original = (await connection.execute(text('SELECT * FROM foreshadow_discoveries'))).mappings().one()
        def migrate(sync):
            with Operations.context(MigrationContext.configure(sync)):
                step = migration('a1001944_foreshadow_rescans')
                step.upgrade(); step.downgrade(); step.upgrade()
        await connection.run_sync(migrate)
        assert (await connection.execute(text('SELECT * FROM foreshadow_discoveries'))).mappings().one() == original
        member = (await connection.execute(text('SELECT * FROM foreshadow_discovery_run_items'))).mappings().one()
        assert dict(member) == dict(run_id=run_id, candidate_id=candidate_id, position=0, reason='原说明')
