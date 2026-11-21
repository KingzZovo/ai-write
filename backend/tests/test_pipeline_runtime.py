"""Whole-book runtime fault injection with real transactions and fake model IO."""
from __future__ import annotations

import asyncio
import contextvars
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db  # Explicit isolated fixture; no app conftest.
from app.models.generation_task import GenerationTask
from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.project import Chapter, Volume
from app.services.pipeline_service import create_pipeline, get_pipeline_status
from app.services.production.control import recover_abandoned, request_pause, request_start
from app.services.production.runtime import run_command


async def queue(r):
    async with r.sessions() as db:
        pipeline, created = await request_start(db, r.pipeline)
        assert created
        command = pipeline.command_id
        await db.commit()
        return command


def canonical(monkeypatch, *, on_call=None, outcome="completed"):
    from app.tasks import generation_tasks
    calls = []
    async def generate(task_id, *, session_factory):
        calls.append(task_id)
        if on_call:
            await on_call(task_id, len(calls))
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json["chapter_id"])
            if outcome == "empty":
                generation_tasks._persist_pipeline_chapter_text(chapter, "")
            elif outcome == "failed":
                task.status, task.error_message = "failed", "模型连接失败"
            else:
                chapter.content_text = f"{chapter.title}的完整正文。"
                chapter.word_count = len(chapter.content_text)
                chapter.status = "completed" if outcome == "completed" else "draft"
                task.result_text = chapter.content_text
                task.status = outcome
            await db.commit()
    monkeypatch.setattr(generation_tasks, "_run_async_generation_impl", generate)
    return calls


async def three_volumes(r):
    async with r.sessions() as db:
        first = (await db.scalars(select(Volume))).one()
        db.add(Chapter(volume_id=first.id, title="卷1章2", chapter_idx=2))
        for index in (2, 3):
            volume = Volume(project_id=r.project, title=f"卷{index}", volume_idx=index)
            db.add(volume)
            await db.flush()
            for chapter_index in (1, 2):
                db.add(Chapter(volume_id=volume.id, title=f"卷{index}章{chapter_index}", chapter_idx=chapter_index))
            await db.flush()
        pipeline = await create_pipeline(db, r.project)
        r.pipeline = pipeline.id
        await db.commit()


@pytest.mark.asyncio
async def test_three_volumes_reach_completed_in_chapter_order(runtime_db, monkeypatch):
    r = runtime_db
    await three_volumes(r)
    calls = canonical(monkeypatch)
    token = await queue(r)
    assert await run_command(r.engine, r.sessions, r.pipeline, token)
    async with r.sessions() as db:
        status = await get_pipeline_status(db, r.pipeline)
        assert status["state"] == "completed" and status["completed_chapters"] == 6
        task_rows = [await db.get(GenerationTask, task_id) for task_id in calls]
        assert [(t.params_json["volume_idx"], t.params_json["chapter_idx"]) for t in task_rows] == [
            (1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2),
        ]
        assert all(row["state"] == "completed" for row in status["chapters"])


@pytest.mark.asyncio
async def test_duplicate_delivery_only_executes_once(runtime_db, monkeypatch):
    r = runtime_db
    calls = canonical(monkeypatch)
    token = await queue(r)
    claimed = await asyncio.gather(
        run_command(r.engine, r.sessions, r.pipeline, token),
        run_command(r.engine, r.sessions, r.pipeline, token),
    )
    assert sum(claimed) == len(calls) == 1
    assert not await run_command(r.engine, r.sessions, r.pipeline, token)


@pytest.mark.asyncio
async def test_pause_finishes_current_chapter_and_resume_skips_it(runtime_db, monkeypatch):
    r = runtime_db
    await three_volumes(r)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_first(task_id, number):
        if number == 1:
            entered.set()
            await release.wait()
    calls = canonical(monkeypatch, on_call=hold_first)
    token = await queue(r)
    task = asyncio.create_task(run_command(r.engine, r.sessions, r.pipeline, token))
    await asyncio.wait_for(entered.wait(), 3)
    async with r.sessions() as db:
        await request_pause(db, r.pipeline)
        await db.commit()
    release.set()
    await asyncio.wait_for(task, 3)
    async with r.sessions() as db:
        status = await get_pipeline_status(db, r.pipeline)
        assert status["state"] == "paused" and status["completed_chapters"] == 1
    new_token = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, new_token)
    assert len(calls) == 6
    async with r.sessions() as db:
        assert (await db.get(PipelineRun, r.pipeline)).state == "completed"


@pytest.mark.asyncio
async def test_paused_queued_message_is_stale(runtime_db, monkeypatch):
    r = runtime_db
    calls = canonical(monkeypatch)
    token = await queue(r)
    async with r.sessions() as db:
        await request_pause(db, r.pipeline)
        await db.commit()
    assert not await run_command(r.engine, r.sessions, r.pipeline, token)
    assert not calls


@pytest.mark.asyncio
async def test_killed_worker_recovered_and_old_message_rejected(runtime_db, monkeypatch):
    r = runtime_db
    entered = asyncio.Event()
    async def hold(task_id, number):
        entered.set()
        await asyncio.Event().wait()
    canonical(monkeypatch, on_call=hold)
    token = await queue(r)
    worker = asyncio.create_task(run_command(r.engine, r.sessions, r.pipeline, token))
    await asyncio.wait_for(entered.wait(), 3)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    async with r.sessions() as db:
        assert await recover_abandoned(db, r.pipeline, stale_seconds=0)
        await db.commit()
        new_token = (await db.get(PipelineRun, r.pipeline)).command_id
    calls = canonical(monkeypatch)
    assert not await run_command(r.engine, r.sessions, r.pipeline, token)
    assert await run_command(r.engine, r.sessions, r.pipeline, new_token)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "empty"])
async def test_model_failure_is_bounded_and_preserves_original(runtime_db, monkeypatch, outcome):
    r = runtime_db
    calls = canonical(monkeypatch, outcome=outcome)
    token = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, token)
    assert len(calls) == 2
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        chapter = await db.get(Chapter, r.chapter)
        assert pipeline.state == "failed" and pipeline.completed_chapters == 0
        assert chapter.content_text == "原稿。"


@pytest.mark.asyncio
async def test_quality_debt_continues_and_remains_visible(runtime_db, monkeypatch):
    r = runtime_db
    await three_volumes(r)
    calls = canonical(monkeypatch, outcome="needs_review")
    token = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, token)
    async with r.sessions() as db:
        status = await get_pipeline_status(db, r.pipeline)
        assert status["state"] == "needs_review" and status["needs_review_chapters"] == 6
        assert status["completed_chapters"] == 0 and len(calls) == 6


@pytest.mark.asyncio
async def test_manual_edit_during_generation_is_not_overwritten(runtime_db, monkeypatch):
    r = runtime_db
    async def manual_edit(task_id, number):
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "作者的新稿。"
            await db.commit()
    async def independent_edit(task_id, number):
        await contextvars.Context().run(asyncio.create_task, manual_edit(task_id, number))
    canonical(monkeypatch, on_call=independent_edit)
    token = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, token)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "作者的新稿。"
        assert (await db.get(PipelineRun, r.pipeline)).state == "failed"


@pytest.mark.asyncio
async def test_heartbeat_updates_during_long_chapter(runtime_db, monkeypatch):
    r = runtime_db
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(task_id, number):
        entered.set()
        await release.wait()
    canonical(monkeypatch, on_call=hold)
    token = await queue(r)
    worker = asyncio.create_task(run_command(r.engine, r.sessions, r.pipeline, token, heartbeat_seconds=0.02))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with r.sessions() as db:
            initial = (await db.get(PipelineRun, r.pipeline)).heartbeat_at
        async def changed():
            while True:
                async with r.sessions() as db:
                    if (await db.get(PipelineRun, r.pipeline)).heartbeat_at > initial:
                        return
                await asyncio.sleep(0.02)
        await asyncio.wait_for(changed(), 3)
    finally:
        release.set()
        await asyncio.wait_for(worker, 3)


@pytest.mark.asyncio
async def test_explicit_retry_gets_fresh_bounded_budget(runtime_db, monkeypatch):
    r = runtime_db
    calls = canonical(monkeypatch, outcome="failed")
    first = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, first)
    second = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, second)
    assert first != second and len(calls) == 4


@pytest.mark.asyncio
async def test_malformed_legacy_retry_option_does_not_strand_worker(runtime_db, monkeypatch):
    r = runtime_db
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        pipeline.options_json = {"max_attempts": "invalid"}
        await db.commit()
    calls = canonical(monkeypatch, outcome="failed")
    token = await queue(r)
    await run_command(r.engine, r.sessions, r.pipeline, token)
    assert len(calls) == 2
    async with r.sessions() as db:
        assert (await db.get(PipelineRun, r.pipeline)).state == "failed"


@pytest.mark.asyncio
async def test_lost_lock_cancels_generation_before_recovery(runtime_db, monkeypatch):
    from app.services.production.locking import WorkerLease, project_worker_lock
    r = runtime_db
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def hold(task_id, number):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def lost(lease):
        await entered.wait()
        raise RuntimeError("项目执行锁已断开")
    canonical(monkeypatch, on_call=hold)
    monkeypatch.setattr(WorkerLease, "check", lost)
    token = await queue(r)
    with pytest.raises(RuntimeError, match="项目执行锁"):
        await asyncio.wait_for(run_command(
            r.engine, r.sessions, r.pipeline, token, heartbeat_seconds=0.01,
        ), 3)
    assert cancelled.is_set()
    async with project_worker_lock(r.engine, r.project) as lease:
        assert lease
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"
        assert await recover_abandoned(db, r.pipeline, stale_seconds=0)
        await db.commit()
