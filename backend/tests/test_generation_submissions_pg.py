"""Real PostgreSQL submission/claim races. Deliberately skipped offline."""
import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.api import generate
from app.models.generation_task import GenerationTask
from app.models.project import Chapter
from app.services.generation_submissions import (
    claim_generation_submission, generation_task_results,
)
from app.services.production.fencing import StaleChapterWrite
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest.fixture
def publisher(monkeypatch):
    publish = Mock()
    monkeypatch.setattr('app.tasks.knowledge_tasks.run_async_generation.apply_async', publish)
    monkeypatch.setattr(generate.asyncio, 'to_thread', AsyncMock(side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs)))
    monkeypatch.setattr('app.services.generation_submissions.validate_submission_outlines', AsyncMock())
    return publish


async def request_for(r):
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        return dict(request_id=str(uuid4()), project_id=str(r.project), task_type='chapter',
                    chapter_id=str(r.chapter), expected_content_text=chapter.content_text or '')


async def submit(r, body):
    async with r.sessions() as db:
        return await generate.start_async_generation(generate.AsyncGenerateRequest(**body), db)


@pytest.mark.asyncio
async def test_parallel_identical_posts_share_one_persistent_request(runtime_db, publisher):
    r = runtime_db
    body = await request_for(r)
    results = await asyncio.gather(submit(r, body), submit(r, body))
    assert {result['task_id'] for result in results} == {body['request_id']}
    async with r.sessions() as db:
        tasks = (await db.scalars(select(GenerationTask).where(GenerationTask.id == UUID(body['request_id'])))).all()
        assert len(tasks) == 1 and tasks[0].params_json['submission']['chapter_source']['chapter_id'] == str(r.chapter)


@pytest.mark.asyncio
async def test_parallel_different_intents_cannot_rebind_the_same_request(runtime_db, publisher):
    r = runtime_db
    body = await request_for(r)
    replies = await asyncio.gather(submit(r, body), submit(r, {**body, 'user_input': '另一份要求'}), return_exceptions=True)
    assert len([reply for reply in replies if isinstance(reply, dict)]) == 1
    error = next(reply for reply in replies if isinstance(reply, HTTPException))
    assert error.status_code == 409 and error.detail['code'] == 'generation_request_conflict'
    publisher.assert_called_once()


@pytest.mark.asyncio
async def test_only_one_database_connection_can_claim_pending_work(runtime_db, publisher):
    r = runtime_db
    body = await request_for(r)
    await submit(r, body)
    async def claim():
        async with r.sessions() as db:
            return await claim_generation_submission(db, body['request_id'])
    assert sum(await asyncio.gather(*(claim() for _ in range(6)))) == 1
    async with r.sessions() as db:
        assert (await db.get(GenerationTask, UUID(body['request_id']))).status == 'running'


@pytest.mark.asyncio
async def test_queued_author_edit_is_not_accepted_as_the_execution_baseline(runtime_db, publisher, monkeypatch):
    from app.db import session
    from app.tasks import generation_tasks
    r = runtime_db
    body = await request_for(r)
    await submit(r, body)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '作者在排队期间补全交钱的动作。'
        await db.commit()
    operation = AsyncMock()
    monkeypatch.setattr(session, 'async_session_factory', r.sessions)
    monkeypatch.setattr(session._state, 'engine', r.engine)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', operation)
    with pytest.raises(StaleChapterWrite, match='提交后'):
        await generation_tasks._run_guarded_generation(body['request_id'])
    operation.assert_not_awaited()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == '作者在排队期间补全交钱的动作。'
        assert (await db.get(GenerationTask, UUID(body['request_id']))).status == 'failed'


@pytest.mark.asyncio
async def test_task_result_guard_preserves_cancel_marker_from_another_connection(runtime_db, publisher, monkeypatch):
    r = runtime_db
    body = await request_for(r)
    await submit(r, body)
    async with r.sessions() as db:
        assert await claim_generation_submission(db, body['request_id'])
    monkeypatch.setattr('app.tasks.celery_app.control.revoke', Mock())
    with generation_task_results(body['request_id']):
        async with r.sessions() as stale:
            task = await stale.get(GenerationTask, UUID(body['request_id']))
            async with r.sessions() as author:
                await generate.cancel_async_generation(body['request_id'], author)
            task.params_json = {**task.params_json, 'late': True}
            task.status = 'completed'
            with pytest.raises(StaleChapterWrite, match='取消'):
                await stale.commit()
            await stale.rollback()
    async with r.sessions() as db:
        task = await db.get(GenerationTask, UUID(body['request_id']))
        assert task.status == 'cancelled' and task.params_json['cancelled_at']
        assert 'late' not in task.params_json
