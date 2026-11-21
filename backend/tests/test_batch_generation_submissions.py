"""Batch selections enter the real submission/worker/source guard offline.

SQLite uses separate connections for author edits. The fixture replaces the
PG lease and generation implementation, not the batch or worker wrapper.
"""
from types import SimpleNamespace as NS
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.models.generation_task import GenerationTask
from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.models.project import Chapter
from app.services.batch_generator import BatchGenerator, BatchStatus
from app.services.batch_execution import BatchLease, using_batch_lease
from app.services.generation_execution import PhysicalConnectionGuard
from app.services.generation_submissions import chapter_submission_source
from tests.test_generation_submissions import book, queued, worker


@pytest.fixture
def batch_worker(worker, monkeypatch):
    for model in (GenerationBatch, GenerationBatchItem): model.__table__.create(worker.engine)
    for chapter in worker.session.scalars(sa.select(Chapter)):
        chapter.outline_json = {'summary': '核对信封，把两千元交给弟弟。'}
    worker.session.commit()
    worker.batch_held, worker.batch_leases, worker.generated_sources = set(), [], {}
    @asynccontextmanager
    async def lease(_db, request_id):
        key = str(request_id)
        if key in worker.batch_held:
            yield None
            return
        worker.batch_held.add(key)
        try:
            with worker.engine.connect() as connection:
                guard = PhysicalConnectionGuard(connection)
                try:
                    # The lock transport is explicitly substituted. The
                    # physical-connection guard and transaction fences are real.
                    actor = BatchLease(key, NS(execute=AsyncMock(), commit=AsyncMock()), guard)
                    worker.batch_leases.append(actor)
                    with using_batch_lease(actor):
                        yield actor
                finally:
                    guard.revoke()
                    connection.invalidate()
                    guard.detach()
        finally:
            worker.batch_held.remove(key)
    worker.batch_lease = lease
    monkeypatch.setattr('app.services.batch_generator.batch_execution_lease', lease)
    monkeypatch.setattr('app.services.batch_recovery.try_command_lock', worker.writer_lock)
    async def implementation(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json['chapter_id'])
            worker.generated_sources[str(chapter.id)] = chapter_submission_source(chapter)
            assert task.params_json['submission']['chapter_source'] == chapter_submission_source(chapter)
            await save_generated(db, task)
    worker.implementation.side_effect = implementation
    return worker


def configs(chapters):
    return [{'chapter_id': str(ch.id), 'expected_content_text': ch.content_text or ''} for ch in chapters]


def batch():
    generator = BatchGenerator()
    generator.hook_manager = NS(
        run_pre_hooks=AsyncMock(return_value=NS(can_proceed=True)),
        run_post_hooks=AsyncMock(), aclose=AsyncMock(),
    )
    return generator


def selection(r):
    return list(r.session.scalars(sa.select(Chapter).order_by(Chapter.chapter_idx).execution_options(populate_existing=True)))


async def save_generated(db, task, text='沈青数出两千元，等弟弟伸手接稳，才松开信封。'):
    chapter = await db.get(Chapter, task.params_json['chapter_id'])
    chapter.content_text, chapter.word_count, chapter.status = text, len(text), 'completed'
    task.result_text, task.progress_text, task.char_count, task.status = text, text, len(text), 'completed'
    await db.commit()


@pytest.mark.asyncio
async def test_batch_captures_all_sources_and_enters_canonical_worker_in_order(batch_worker):
    worker = batch_worker
    chapters = selection(worker)
    expected = {str(ch.id): chapter_submission_source(ch) for ch in chapters}
    calls = []

    async def generate(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            assert task.status == 'running'
            cid = task.params_json['chapter_id']
            assert task.params_json['submission']['chapter_source'] == expected[cid]
            calls.append(cid)
            await save_generated(db, task, '任务生成的新稿。')

    worker.implementation.side_effect = generate
    async with worker.sessions() as db:
        job = await batch().generate_batch(str(worker.pid),
            configs(reversed(chapters)), db=db, request_id=uuid4())
    assert job.status == BatchStatus.COMPLETED and job.completed_chapters == 2
    assert calls == [str(ch.id) for ch in chapters]
    worker.session.expire_all()
    assert all(ch.content_text == '任务生成的新稿。' for ch in chapters)


@pytest.mark.asyncio
@pytest.mark.parametrize('field, value', [
    ('content_text', '作者在第一章生成期间改写了第二章。'),
    ('outline_json', {'summary': '作者重新安排了交钱与求医的先后。'}),
    ('title', '作者刚改的章名'),
])
async def test_waiting_batch_chapter_cannot_adopt_a_later_author_edit(batch_worker, field, value):
    worker = batch_worker
    first, second = selection(worker)
    second_id = second.id
    original_source = chapter_submission_source(second)

    async def generate(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            assert task.params_json['chapter_id'] == str(first.id)
            await save_generated(db, task)
        # A separate author connection changes the chapter still waiting.
        with worker.engine.begin() as connection:
            connection.execute(sa.update(Chapter).where(Chapter.id == second_id).values({field: value}))

    worker.implementation.side_effect = generate
    async with worker.sessions() as db:
        job = await batch().generate_batch(str(worker.pid),
            configs((first, second)), db=db, request_id=uuid4())
    assert worker.implementation.await_count == 1
    assert job.status == BatchStatus.ERROR and job.completed_chapters == 1
    assert job.results[1].status == 'error' and '提交后' in job.results[1].error
    worker.session.expire_all()
    assert getattr(worker.session.get(Chapter, second_id), field) == value
    task = worker.session.get(GenerationTask, UUID(job.results[1].generation_task_id))
    assert task.status == 'failed'
    assert task.params_json['submission']['chapter_source'] == original_source


@pytest.mark.asyncio
async def test_batch_resolves_committed_source_instead_of_cached_chapter(batch_worker):
    worker = batch_worker
    async with worker.sessions() as db:
        cached = await db.get(Chapter, worker.cid)
        old = cached.content_text
        with worker.engine.begin() as connection:
            connection.execute(sa.update(Chapter).where(Chapter.id == worker.cid)
                .values(content_text='另一连接在提交批量任务前已经保存的正文。'))
        assert cached.content_text == old
        job = await batch().generate_batch(str(worker.pid), [{'chapter_id': str(worker.cid), 'expected_content_text': '另一连接在提交批量任务前已经保存的正文。'}], db=db, request_id=uuid4())
        assert job.status == BatchStatus.COMPLETED
        task = await db.get(GenerationTask, job.results[0].generation_task_id)
        chapter = await db.get(Chapter, worker.cid)
        assert task.params_json['submission']['chapter_source'] == worker.generated_sources[str(chapter.id)]
    worker.implementation.assert_awaited_once()
