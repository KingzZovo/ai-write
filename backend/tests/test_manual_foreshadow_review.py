from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update, text
from sqlalchemy.dialects import postgresql
from app.models.project import ForeshadowReviewJob as Job, Chapter, ForeshadowReview
from app.services.entity_sources import resolve_entity_source
from app.services.foreshadow_review_jobs import create_review_job, ReviewSourceNotReady
from tests.test_foreshadow_review_worker import runtime, seed
from tests.test_foreshadow_review_store import database


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['draft', 'needs_review', 'completed', 'generating', 'empty'])
async def test_draft_policy_is_explicit_and_keeps_entity_default_completed_only(status):
    row = NS(id=uuid4(), title='正文', content_text='保存稿', status=status, chapter_idx=1,
             global_idx=2, volume_id=uuid4(), project_id=uuid4(), volume_idx=1)
    db = NS(execute=AsyncMock(side_effect=[NS(all=lambda: [row]), NS(scalars=lambda: NS(all=lambda: [row.id]))]))
    assert bool(await resolve_entity_source(db, row.project_id, 1, row.id)) == (status == 'completed')
    db.execute.reset_mock(side_effect=True)
    db.execute.side_effect = [NS(all=lambda: [row]), NS(scalars=lambda: NS(all=lambda: [row.id]))]
    assert bool(await resolve_entity_source(db, row.project_id, 1, row.id, allow_review_drafts=True)) == (status in {'draft', 'needs_review', 'completed'})
    sql = str(db.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert 'volumes.project_id' in sql and 'chapters.id =' in sql


@pytest.fixture
def manual(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr('app.services.entity_sources.resolve_entity_source', r.resolver)
    original = r.db.execute
    r.sequence_calls = []
    async def execute(stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "nextval('entity_source_outbox_revisions')" in sql:
            r.sequence_calls.append(sql)
            return NS(scalar_one=lambda: 1000 + len(r.sequence_calls))
        return await original(stmt)
    r.db.execute = execute
    return r


@pytest.mark.asyncio
async def test_manual_request_reuses_active_job_and_new_cycle_uses_shared_revision(manual):
    r = manual
    pid, cid = UUID(r.review.project_id), UUID(r.review.chapter_id)
    with r.session.begin(): first = await create_review_job(r.db, pid, cid, r.source.content)
    with r.session.begin(): repeated = await create_review_job(r.db, pid, cid, r.source.content)
    assert first == repeated and len(r.sequence_calls) == 1
    r.resolver.assert_awaited_with(r.db, pid, 0, cid, lock=True, allow_review_drafts=True)
    r.session.execute(update(Job).values(status='completed')); r.session.commit()
    with r.session.begin(): fresh = await create_review_job(r.db, pid, cid, r.source.content)
    assert fresh['id'] != first['id']
    assert r.session.execute(select(Job.source_revision).order_by(Job.source_revision)).scalars().all() == [1001, 1002]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['unsaved', 'unavailable'])
async def test_invalid_source_never_allocates_or_enqueues(manual, case):
    r = manual
    if case == 'unavailable': r.resolver.return_value = None
    with pytest.raises(ReviewSourceNotReady):
        with r.session.begin():
            await create_review_job(r.db, UUID(r.review.project_id), UUID(r.review.chapter_id), 'unsaved')
    assert r.sequence_calls == []
    assert r.session.execute(select(Job.id)).all() == []


@pytest.mark.asyncio
async def test_manual_route_commits_before_ack_and_propagates_conflict(manual):
    from app.api import foreshadows as api
    r = manual
    async def commit(): r.session.commit()
    adapter = NS(execute=r.db.execute, commit=AsyncMock(side_effect=commit))
    async def provide_db(): return adapter
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide_db
    body = {'chapter_id': r.review.chapter_id, 'expected_content': r.source.content}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = f'/api/projects/{r.review.project_id}/foreshadows/review-jobs'
        response = await client.post(url, json=body)
        assert response.status_code == 200 and response.json()['chapter_id'] == r.review.chapter_id
        adapter.commit.assert_awaited_once()
        response = await client.post(url, json={**body, 'expected_content': '未保存修改'})
        assert response.status_code == 409
        adapter.commit.assert_awaited_once()
        response = await client.post(url, json={**body, 'chapter_id': 'bad-id'})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_worker_explicitly_allows_review_drafts(runtime):
    from app.services import foreshadow_review_worker as worker
    from dataclasses import replace
    r = runtime; job_id = await seed(r)
    r.session.execute(update(Chapter).values(status='draft')); r.session.commit()
    updated_at = r.session.execute(select(Chapter.updated_at)).scalar_one(); r.session.commit()
    r.reviewer.return_value = replace(r.review, source_status='draft', source_updated_at=updated_at)
    await worker.run_review_job(job_id, sessions=r.sessions)
    assert all(call.kwargs.get('allow_review_drafts') is True for call in r.resolver.await_args_list)
    assert r.session.execute(select(ForeshadowReview.source_snapshot)).scalar_one()['source_status'] == 'draft'


@pytest.mark.asyncio
async def test_newer_outbox_prevents_reusing_old_active_job(manual):
    r = manual
    await seed(r, 100)
    r.session.execute(text('INSERT INTO entity_source_outbox (chapter_id, revision) VALUES (:id, 200)'),
                      {'id': UUID(r.review.chapter_id).hex})
    r.session.commit()
    with r.session.begin():
        result = await create_review_job(r.db, UUID(r.review.project_id), UUID(r.review.chapter_id), r.source.content)
    assert len(r.sequence_calls) == 1
    assert r.session.execute(select(Job.source_revision).where(Job.id == UUID(result['id']))).scalar_one() > 200
