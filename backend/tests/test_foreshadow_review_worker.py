from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update, text
from sqlalchemy.orm import Session
from app.models.project import (ForeshadowReviewJob as Job, ForeshadowReview, Foreshadow, ForeshadowDiscoveryRun,
    ForeshadowDiscovery, ForeshadowDiscoveryRunItem, ForeshadowReviewRequest, _utcnow)
from app.services.foreshadow_review_jobs import enqueue_review_job
from app.services import foreshadow_review_worker as worker
from tests.test_foreshadow_review_store import database


@pytest.fixture
def runtime(database, monkeypatch):
    session, db, review, _ = database
    engine = session.get_bind()
    Job.__table__.create(engine)
    ForeshadowDiscoveryRun.__table__.create(engine)
    ForeshadowDiscovery.__table__.create(engine)
    ForeshadowDiscoveryRunItem.__table__.create(engine)
    ForeshadowReviewRequest.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE entity_source_outbox (chapter_id UUID PRIMARY KEY, revision BIGINT NOT NULL)'))
    source = NS(project_id=review.project_id, chapter_id=review.chapter_id, source_hash=review.source_sha256, global_idx=913,
                content='陈伯把寄信的存根放在桌上。', receipt={'volume_id': review.volume_id, 'chapter_idx': 3})
    @asynccontextmanager
    async def sessions():
        with Session(engine) as store:
            async def execute(statement): return store.execute(statement)
            async def commit(): store.commit()
            yield NS(execute=execute, commit=commit)
    resolver, reviewer = AsyncMock(return_value=source), AsyncMock(return_value=review)
    monkeypatch.setattr(worker, 'resolve_entity_source', resolver)
    monkeypatch.setattr(worker, 'review_scoped_chapter', reviewer)
    from app.services.foreshadow_discovery import DiscoveryBatch, source_snapshot
    from app.services.entity_sources import digest
    snapshot = source_snapshot(source)
    detector = AsyncMock(return_value=DiscoveryBatch(snapshot, digest(snapshot)))
    monkeypatch.setattr(worker, 'detect_clues', detector)
    return NS(session=session, db=db, review=review, source=source, sessions=sessions,
              resolver=resolver, reviewer=reviewer, detector=detector, engine=engine)


async def seed(r, revision=100):
    with r.session.begin(): await enqueue_review_job(r.db, r.source, revision)
    job_id = r.session.execute(select(Job.id).where(Job.source_revision == revision)).scalar_one()
    r.session.commit()
    return job_id


@pytest.mark.asyncio
async def test_worker_saves_and_completes_once(runtime):
    r = runtime; job_id = await seed(r)
    assert await worker.run_review_job(job_id, sessions=r.sessions) == {'status': 'completed', 'saved': 1, 'discovered': 0}
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'not_claimed'
    assert r.session.execute(select(Job.status, Job.attempts)).one() == ('completed', 1)
    assert r.session.execute(select(ForeshadowReview.status)).scalar_one() == 'pending'
    r.reviewer.assert_awaited_once()


@pytest.mark.asyncio
async def test_newer_job_makes_old_work_obsolete_before_model(runtime):
    r = runtime; old = await seed(r); await seed(r, 101)
    assert (await worker.run_review_job(old, sessions=r.sessions))['status'] == 'obsolete'
    r.reviewer.assert_not_called()


@pytest.mark.asyncio
async def test_model_failure_is_not_completed_or_saved(runtime):
    r = runtime; job_id = await seed(r)
    r.reviewer.side_effect = RuntimeError('provider secret details')
    with pytest.raises(RuntimeError): await worker.run_review_job(job_id, sessions=r.sessions)
    assert r.session.execute(select(Job.status, Job.last_error)).one() == ('failed', 'RuntimeError')
    assert r.session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_replaced_attempt_cannot_save_or_overwrite_new_owner(runtime):
    r = runtime; job_id = await seed(r); new_token = uuid4()
    async def infer(**kwargs):
        with Session(r.engine) as db:
            db.execute(update(Job).values(attempt_token=new_token))
            db.commit()
        return r.review
    r.reviewer.side_effect = infer
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'superseded'
    assert r.session.execute(select(Job.attempt_token)).scalar_one() == new_token
    assert r.session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_foreshadow_changed_after_review_fails_instead_of_completing_empty(runtime):
    r = runtime; job_id = await seed(r)
    async def infer(**kwargs):
        with Session(r.engine) as db:
            db.execute(update(Foreshadow).values(description='新悬念'))
            db.commit()
        return r.review
    r.reviewer.side_effect = infer
    with pytest.raises(ValueError): await worker.run_review_job(job_id, sessions=r.sessions)
    assert r.session.execute(select(Job.status)).scalar_one() == 'failed'
    assert r.session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_final_job_write_failure_rolls_back_inserted_reviews(runtime, monkeypatch):
    r = runtime; job_id = await seed(r)
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            execute = db.execute
            async def fail_final(statement):
                if str(statement).startswith('UPDATE foreshadow_review_jobs') and statement.compile().params.get('status') == 'completed':
                    raise RuntimeError('final write failed')
                return await execute(statement)
            yield NS(execute=fail_final, commit=db.commit)
    with pytest.raises(RuntimeError): await worker.run_review_job(job_id, sessions=sessions)
    assert r.session.execute(select(ForeshadowReview)).all() == []
    assert r.session.execute(select(Job.status)).scalar_one() == 'failed'


@pytest.mark.asyncio
async def test_recovery_rotates_dispatch_failures_and_caps_abandoned_attempts(runtime):
    r = runtime; job_id = await seed(r)
    def fail_dispatch(_): raise RuntimeError('broker down')
    assert (await worker.replay_review_jobs(r.sessions, dispatch=fail_dispatch))['sent'] == 0
    assert (await worker.replay_review_jobs(r.sessions, dispatch=fail_dispatch))['examined'] == 0
    r.session.execute(update(Job).values(status='running', attempts=3, updated_at=_utcnow() - timedelta(minutes=11)))
    r.session.commit()
    dispatch = []
    await worker.replay_review_jobs(r.sessions, dispatch=dispatch.append)
    assert dispatch == []
    assert r.session.execute(select(Job.status, Job.last_error)).one() == ('failed', 'attempts_exhausted')


@pytest.mark.asyncio
async def test_completed_batch_hook_defers_to_source_outbox(runtime, monkeypatch):
    from app.services import foreshadow_review_jobs as jobs
    create = AsyncMock()
    monkeypatch.setattr(jobs, 'create_review_job', create)
    r = runtime
    assert await jobs.enqueue_saved_draft_review(r.review.project_id, r.review.volume_id, 3,
        r.source.content, sessions=r.sessions) is None
    create.assert_not_called()


@pytest.mark.asyncio
async def test_pending_source_notification_supersedes_job_even_before_new_job_exists(runtime):
    r = runtime; job_id = await seed(r)
    r.session.execute(text('INSERT INTO entity_source_outbox (chapter_id, revision) VALUES (:id, 101)'),
                      {'id': UUID(r.review.chapter_id).hex})
    r.session.commit()
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'obsolete'
    r.reviewer.assert_not_called()


def test_review_tasks_are_registered_for_periodic_recovery():
    from app.tasks import celery_app
    assert 'foreshadows.review_chapter' in celery_app.tasks
    assert 'foreshadows.replay_reviews' in celery_app.tasks
    assert celery_app.conf.beat_schedule['foreshadow-review-recovery']['task'] == 'foreshadows.replay_reviews'


@pytest.mark.asyncio
async def test_new_source_notification_during_inference_prevents_final_save(runtime):
    r = runtime; job_id = await seed(r)
    async def infer(**kwargs):
        with Session(r.engine) as db:
            db.execute(text('INSERT INTO entity_source_outbox (chapter_id, revision) VALUES (:id, 101)'),
                       {'id': UUID(r.review.chapter_id).hex})
            db.commit()
        return r.review
    r.reviewer.side_effect = infer
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'obsolete'
    assert r.session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_cancellation_records_failure_without_saving(runtime):
    import asyncio
    r = runtime; job_id = await seed(r)
    r.reviewer.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await worker.run_review_job(job_id, sessions=r.sessions)
    assert r.session.execute(select(Job.status, Job.last_error)).one() == ('failed', 'CancelledError')
    assert r.session.execute(select(ForeshadowReview)).all() == []


@pytest.mark.asyncio
async def test_recovery_dispatches_an_expired_attempt_and_worker_can_complete(runtime):
    r = runtime; job_id = await seed(r)
    r.session.execute(update(Job).values(status='running', attempts=1, updated_at=_utcnow() - timedelta(minutes=11)))
    r.session.commit()
    sent = []
    assert (await worker.replay_review_jobs(r.sessions, dispatch=sent.append))['sent'] == 1
    assert sent == [str(job_id)]
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'completed'
    assert r.session.execute(select(Job.attempts)).scalar_one() == 2


@pytest.mark.asyncio
async def test_job_listing_shows_latest_revision_and_hides_internal_payload(runtime):
    from app.services.foreshadow_review_jobs import list_review_jobs
    r = runtime; await seed(r); latest = await seed(r, 101)
    result = await list_review_jobs(r.db, UUID(r.review.project_id))
    assert [j['id'] for j in result['jobs']] == [latest]
    assert 'source_json' not in result['jobs'][0] and 'last_error' not in result['jobs'][0]
    assert result['jobs'][0]['chapter_title'] == '信'
    assert (await list_review_jobs(r.db, uuid4()))['jobs'] == []


@pytest.mark.asyncio
async def test_author_retry_resets_failed_cycle_and_invalidates_old_token(runtime, monkeypatch):
    from app.services.foreshadow_review_jobs import retry_review_job, ReviewJobNotRetryable
    r = runtime; job_id = await seed(r)
    monkeypatch.setattr('app.services.entity_sources.resolve_entity_source', r.resolver)
    r.session.execute(update(Job).values(status='failed', attempts=3, attempt_token=uuid4(), last_error='TimeoutError'))
    r.session.commit()
    with r.session.begin(): await retry_review_job(r.db, UUID(r.review.project_id), job_id)
    assert r.session.execute(select(Job.status, Job.attempts, Job.attempt_token, Job.last_error)).one() == ('pending', 0, None, None)
    r.session.commit()
    with pytest.raises(ReviewJobNotRetryable):
        with r.session.begin(): await retry_review_job(r.db, UUID(r.review.project_id), job_id)
    assert (await worker.run_review_job(job_id, sessions=r.sessions))['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'new_revision', 'foreign_project', 'running'])
async def test_author_retry_rejects_stale_foreign_or_running_work(runtime, monkeypatch, change):
    from app.services.foreshadow_review_jobs import retry_review_job, ReviewJobNotRetryable, ReviewJobNotFound
    r = runtime; job_id = await seed(r)
    monkeypatch.setattr('app.services.entity_sources.resolve_entity_source', r.resolver)
    r.session.execute(update(Job).values(status='failed', attempts=3))
    r.session.commit()
    if change == 'source': r.resolver.return_value = None
    if change == 'new_revision': await seed(r, 101)
    if change == 'running':
        r.session.execute(update(Job).values(status='running'))
        r.session.commit()
    with pytest.raises(ReviewJobNotFound if change == 'foreign_project' else ReviewJobNotRetryable):
        with r.session.begin():
            await retry_review_job(r.db, uuid4() if change == 'foreign_project' else UUID(r.review.project_id), job_id)
    assert r.session.execute(select(Job.attempts).where(Job.id == job_id)).scalar_one() == 3


@pytest.mark.asyncio
async def test_job_routes_validate_scope_and_commit_retry(runtime, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api import foreshadows as api
    r = runtime; job_id = await seed(r)
    monkeypatch.setattr('app.services.entity_sources.resolve_entity_source', r.resolver)
    r.session.execute(update(Job).values(status='failed', attempts=3, last_error='private-provider-detail'))
    r.session.commit()
    async def commit(): r.session.commit()
    adapter = NS(execute=r.db.execute, commit=AsyncMock(side_effect=commit))
    async def provide_db(): return adapter
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        base = f'/api/projects/{r.review.project_id}/foreshadows/review-jobs'
        listed = await client.get(base)
        assert listed.status_code == 200 and 'private-provider-detail' not in listed.text
        assert (await client.get(base + '?offset=-1')).status_code == 422
        result = await client.post(base + f'/{job_id}/retry')
        assert result.status_code == 200 and result.json()['status'] == 'pending'
        adapter.commit.assert_awaited_once()
        assert (await client.post(base + f'/{job_id}/retry')).status_code == 409
