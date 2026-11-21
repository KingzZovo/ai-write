"""Real PostgreSQL request and claim races, intentionally not run offline."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.generation_task import GenerationTask
from app.models.project import Chapter
from app.services.batch_submissions import BatchSubmissionError, batch_status, claim_batch, submit_batch
from app.services.batch_execution import batch_execution_lease
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def batch_book(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as connection:
        for model in (GenerationBatch, GenerationBatchItem):
            await connection.run_sync(lambda db: model.__table__.create(db))
    monkeypatch.setattr('app.services.generation_submissions.validate_submission_outlines', AsyncMock())
    r.batch_id = uuid4()
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        r.batch_configs = [{'chapter_id': str(chapter.id), 'expected_content_text': chapter.content_text or ''}]
    return r


async def submit(r, style=''):
    async with r.sessions() as db:
        return await submit_batch(db, r.project, r.batch_id, r.batch_configs, style)


@pytest.mark.asyncio
async def test_parallel_batch_submissions_create_one_manifest_and_one_child(batch_book):
    r = batch_book
    replies = await asyncio.gather(submit(r), submit(r))
    assert replies == [str(r.batch_id), str(r.batch_id)]
    async with r.sessions() as db:
        for model in (GenerationBatch, GenerationBatchItem, GenerationTask):
            assert await db.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.asyncio
async def test_same_batch_number_cannot_bind_concurrent_different_instructions(batch_book):
    r = batch_book
    replies = await asyncio.gather(submit(r, '先交钱'), submit(r, '先背人'), return_exceptions=True)
    assert len([reply for reply in replies if isinstance(reply, str)]) == 1
    failure = next(reply for reply in replies if isinstance(reply, BatchSubmissionError))
    assert failure.code == 'batch_request_conflict'


@pytest.mark.asyncio
async def test_parallel_batch_claims_grant_one_execution(batch_book):
    r = batch_book
    await submit(r)
    async def claim():
        async with r.sessions() as db:
            async with batch_execution_lease(db, r.batch_id) as lease:
                return await claim_batch(db, r.project, r.batch_id) if lease else None
    assert sum(value is not None for value in await asyncio.gather(*(claim() for _ in range(6)))) == 1


@pytest.mark.asyncio
async def test_lost_pg_acceptance_ack_keeps_original_tasks_after_author_edit(batch_book):
    r = batch_book
    async with r.sessions() as db:
        commit = db.commit
        async def lose_ack():
            await commit()
            raise RuntimeError('test lost acknowledgement')
        db.commit = lose_ack
        with pytest.raises(BatchSubmissionError) as failure:
            await submit_batch(db, r.project, r.batch_id, r.batch_configs)
        assert failure.value.http_status == 503
    async with r.sessions() as db:
        (await db.get(Chapter, r.chapter)).content_text = '作者后来的新稿。'
        await db.commit()
    assert await submit(r) == str(r.batch_id)
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(GenerationTask)) == 1
        assert (await db.get(Chapter, r.chapter)).content_text == '作者后来的新稿。'


@pytest.mark.asyncio
async def test_new_reader_sees_committed_batch_without_reading_chapter_text(batch_book):
    r = batch_book
    await submit(r)
    async with r.sessions() as db:
        saved = await batch_status(db, r.project, r.batch_id)
    assert saved['status'] == 'pending' and saved['total'] == 1
    assert saved['results'][0]['chapter_id'] == str(r.chapter)
    assert saved['results'][0]['generation_task_id']
