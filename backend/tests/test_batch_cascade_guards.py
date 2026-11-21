"""Batch/cascade callers exercise the same real project mutex as prose workers."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from tests.test_pipeline_runtime import canonical, three_volumes
from app.db import session
from app.models.project import Chapter, ChapterEvaluation, Volume
from app.models.cascade_task import CascadeTask
from app.models.generation_task import GenerationTask
from app.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.services.batch_generator import BatchGenerator, BatchStatus
from app.services.production.control import request_start
from app.services.production.writer import guarded_call
from app.tasks import cascade


@pytest_asyncio.fixture
async def configured(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as connection:
        for model in (GenerationBatch, GenerationBatchItem):
            await connection.run_sync(lambda db: model.__table__.create(db))
    # These real-PG cases isolate writer/claim behavior; HTTP readiness is
    # separately covered by the actual SQLite application route tests.
    monkeypatch.setattr('app.services.generation_submissions.validate_submission_outlines', AsyncMock())
    monkeypatch.setattr(session, "async_session_factory", r.sessions)
    monkeypatch.setattr(session._state, "engine", r.engine)
    return r


def batch():
    generator = BatchGenerator()
    generator.hook_manager = SimpleNamespace(
        run_pre_hooks=AsyncMock(return_value=SimpleNamespace(can_proceed=True)),
        run_post_hooks=AsyncMock(), aclose=AsyncMock(),
    )
    return generator
async def batch_configs(db, ids):
    output = []
    for cid in ids:
        chapter = await db.get(Chapter, cid)
        output.append({'chapter_id': str(cid), 'expected_content_text': chapter.content_text or '' if chapter else ''})
    return output


async def batch_response(r, ids):
    from app.api import rewrite
    async with r.sessions() as db:
        body = rewrite.BatchGenerateRequest(project_id=str(r.project), request_id=uuid4(),
            chapter_configs=await batch_configs(db, ids))
        return await rewrite.batch_generate(body, db)


@pytest.mark.asyncio
async def test_batch_uses_canonical_chain_in_cross_volume_order(configured, monkeypatch):
    r = configured
    await three_volumes(r)
    calls = canonical(monkeypatch)
    async with r.sessions() as db:
        chapters = list((await db.scalars(select(Chapter).join(Volume).order_by(Volume.volume_idx, Chapter.chapter_idx))).all())
        expected = [str(ch.id) for ch in chapters]
        job = await batch().generate_batch(str(r.project), await batch_configs(db, reversed(expected)), db=db, request_id=uuid4())
        assert job.status == BatchStatus.COMPLETED and job.completed_chapters == 6
        tasks = [await db.get(GenerationTask, task_id) for task_id in calls]
        assert [task.params_json["chapter_id"] for task in tasks] == expected


@pytest.mark.asyncio
async def test_batch_quality_debt_is_not_completed(configured, monkeypatch):
    r = configured
    canonical(monkeypatch, outcome="needs_review")
    async with r.sessions() as db:
        job = await batch().generate_batch(str(r.project), await batch_configs(db, [r.chapter]), db=db, request_id=uuid4())
        assert job.status == BatchStatus.NEEDS_REVIEW and job.completed_chapters == 0
        assert job.results[0].status == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["duplicate", "foreign"])
async def test_batch_validates_all_targets_before_model_calls(configured, monkeypatch, bad):
    r = configured
    calls = canonical(monkeypatch)
    ids = [r.chapter, r.chapter if bad == "duplicate" else uuid4()]
    async with r.sessions() as db:
        with pytest.raises(ValueError):
            await batch().generate_batch(str(r.project), await batch_configs(db, ids), db=db, request_id=uuid4())
    assert not calls


@pytest.mark.asyncio
async def test_batch_cannot_write_while_pipeline_queued(configured, monkeypatch):
    r = configured
    calls = canonical(monkeypatch)
    async with r.sessions() as db:
        await request_start(db, r.pipeline)
        await db.commit()
        job = await batch().generate_batch(str(r.project), await batch_configs(db, [r.chapter]), db=db, request_id=uuid4())
        assert job.status == BatchStatus.ERROR and not calls
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest_asyncio.fixture
async def cascade_row(configured):
    r = configured
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda db: ChapterEvaluation.__table__.create(db))
        await conn.run_sync(lambda db: CascadeTask.__table__.create(db))
    async with r.sessions() as db:
        evaluation = ChapterEvaluation(chapter_id=r.chapter, overall=4)
        db.add(evaluation)
        await db.flush()
        row = CascadeTask(project_id=r.project, source_chapter_id=r.chapter,
                          source_evaluation_id=evaluation.id, target_entity_type="chapter",
                          target_entity_id=r.chapter, severity="high", issue_summary="补充因果")
        db.add(row)
        await db.commit()
        return r, str(row.id)


@pytest.mark.asyncio
async def test_cascade_defers_to_shared_writer_without_claim(cascade_row):
    r, task_id = cascade_row
    async def busy():
        result = await cascade._run_cascade_task_async(task_id)
        assert result["status"] == "deferred"
        async with r.sessions() as db:
            assert (await db.get(CascadeTask, task_id)).status == "pending"
    await guarded_call(r.engine, r.sessions, r.project, busy, chapter_id=r.chapter)
    result = await cascade._run_cascade_task_async(task_id)
    assert result["status"] == "done"
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        assert chapter.content_text == "原稿。"
        assert chapter.outline_json["outline_contract_repair_required"]


@pytest.mark.asyncio
async def test_cascade_recovers_abandoned_claim_without_duplicate_revision(cascade_row):
    r, task_id = cascade_row
    async with r.sessions() as db:
        row = await db.get(CascadeTask, task_id)
        row.status = "running"
        await db.commit()
    assert (await cascade._run_cascade_task_async(task_id))["status"] == "done"
    assert (await cascade._run_cascade_task_async(task_id))["status"] == "skipped"
    async with r.sessions() as db:
        assert len((await db.get(Chapter, r.chapter)).outline_json["cascade_revisions"]) == 1


@pytest.mark.asyncio
async def test_batch_sse_reports_each_volume_local_chapter(configured, monkeypatch):
    from app.api import rewrite
    from app.services import batch_generator
    r = configured
    await three_volumes(r)
    canonical(monkeypatch)
    monkeypatch.setattr(rewrite, "async_session_factory", r.sessions)
    generator = batch()
    monkeypatch.setattr(batch_generator, "BatchGenerator", lambda: generator)
    async with r.sessions() as db:
        ids = list((await db.scalars(select(Chapter.id))).all())
    response = await batch_response(r, ids)
    text = "".join([frame async for frame in response.body_iterator])
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    chapters = [event for event in events if "chapter_id" in event]
    assert len(chapters) == 6 and len({event["chapter_id"] for event in chapters}) == 6
    assert [(event["volume_idx"], event["chapter"]) for event in chapters] == [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)]
    assert events[-1]["status"] == "completed" and text.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_batch_sse_needs_review_and_failure_terminate_honestly(configured, monkeypatch, invalid):
    from app.api import rewrite
    from app.services import batch_generator
    r = configured
    canonical(monkeypatch, outcome="needs_review")
    monkeypatch.setattr(rewrite, "async_session_factory", r.sessions)
    generator = batch()
    monkeypatch.setattr(batch_generator, "BatchGenerator", lambda: generator)
    if invalid:
        with pytest.raises(HTTPException) as failure:
            await batch_response(r, [uuid4()])
        assert failure.value.status_code == 404
        return
    response = await batch_response(r, [r.chapter])
    text = "".join([frame async for frame in response.body_iterator])
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    assert text.endswith("data: [DONE]\n\n")
    assert events[-1]["status"] == "needs_review" and events[-1]["completed"] == 0


@pytest.mark.asyncio
async def test_batch_disconnect_drains_generation_and_marks_task_cancelled(configured, monkeypatch):
    from app.api import rewrite
    from app.services import batch_generator
    r = configured
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def hold(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    canonical(monkeypatch, on_call=hold)
    monkeypatch.setattr(rewrite, "async_session_factory", r.sessions)
    generator = batch()
    monkeypatch.setattr(batch_generator, "BatchGenerator", lambda: generator)
    response = await batch_response(r, [r.chapter])
    stream = response.body_iterator
    await anext(stream)  # starting
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()
    assert stopped.is_set()
    async with r.sessions() as db:
        task = (await db.scalars(select(GenerationTask))).one()
        assert task.status == "cancelled"
        await request_start(db, r.pipeline)
        await db.commit()


@pytest.mark.asyncio
async def test_cascade_handler_errors_retry_with_bounded_attempts(cascade_row, monkeypatch):
    r, task_id = cascade_row
    handler = AsyncMock(side_effect=RuntimeError("transient failure"))
    monkeypatch.setattr(cascade, "_handle_target", handler)
    for attempt in range(1, 5):
        with pytest.raises(RuntimeError, match="transient"):
            await cascade._run_cascade_task_async(task_id)
        async with r.sessions() as db:
            row = await db.get(CascadeTask, task_id)
            assert row.status == ("pending" if attempt < 4 else "failed")
            assert row.attempt_count == attempt
    assert (await cascade._run_cascade_task_async(task_id))["status"] == "skipped"
    assert handler.await_count == 4
