"""Real transaction/concurrency tests, exclusively in a disposable test schema."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline, Project, Volume
from app.services.pipeline_service import create_pipeline
from app.services.production.control import (
    PipelineConflict, content_hash, recover_abandoned, request_pause,
    request_rollback, request_start,
)
from app.models.cognition import ChapterCognition
from app.services.production.locking import project_worker_lock, try_command_lock


@pytest_asyncio.fixture
async def runtime_db():
    url = os.environ.get("PIPELINE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires explicit disposable PIPELINE_TEST_DATABASE_URL")
    parsed = make_url(url)
    if parsed.database != "aiwrite_pipeline_test" or parsed.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Refusing to use a non-test database")
    schema = "pipeline_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                Project.__table__, Volume.__table__, Chapter.__table__, Outline.__table__,
                ChapterCognition.__table__,
                PipelineRun.__table__, PipelineChapterStatus.__table__,
                GenerationTask.__table__,
            ]))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            project = Project(title="隔离并发测试")
            db.add(project)
            await db.flush()
            volume = Volume(project_id=project.id, title="第一卷", volume_idx=1)
            db.add(volume)
            await db.flush()
            chapter = Chapter(volume_id=volume.id, title="第一章", chapter_idx=1,
                              content_text="原稿。", word_count=3, status="draft", summary="原摘要")
            db.add(chapter)
            await db.flush()
            pipeline = await create_pipeline(db, project.id)
            await db.commit()
        yield SimpleNamespace(engine=engine, sessions=sessions, project=project.id,
                              pipeline=pipeline.id, chapter=chapter.id, schema=schema)
    finally:
        await engine.dispose()
        # Only this test-created random schema; never any application schema.
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_start_is_durable_and_duplicate_does_not_change_snapshot(runtime_db):
    r = runtime_db
    async with r.sessions() as db:
        pipeline, queued = await request_start(db, r.pipeline)
        assert queued and pipeline.state == "queued"
        command = pipeline.command_id
        snapshot = pipeline.snapshot_json
        await db.commit()
    async with r.sessions() as db:
        pipeline, queued = await request_start(db, r.pipeline)
        assert not queued and pipeline.command_id == command and pipeline.snapshot_json == snapshot


@pytest.mark.asyncio
async def test_concurrent_start_queues_only_once(runtime_db):
    async def start():
        async with runtime_db.sessions() as db:
            try:
                _, queued = await request_start(db, runtime_db.pipeline)
                await db.commit()
                return queued
            except PipelineConflict:
                return False
    assert sum(await asyncio.gather(start(), start())) == 1


@pytest.mark.asyncio
async def test_other_pipeline_for_same_book_cannot_start(runtime_db):
    r = runtime_db
    async with r.sessions() as db:
        other = await create_pipeline(db, r.project)
        other_id = other.id
        await request_start(db, r.pipeline)
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(PipelineConflict, match="已有"):
            await request_start(db, other_id)


@pytest.mark.asyncio
async def test_queued_pause_invalidates_delivery_and_can_resume(runtime_db):
    r = runtime_db
    async with r.sessions() as db:
        pipeline, _ = await request_start(db, r.pipeline)
        token = pipeline.command_id
        await db.commit()
        pipeline = await request_pause(db, r.pipeline)
        assert pipeline.state == "paused" and pipeline.command_id != token
        await db.commit()
        pipeline, queued = await request_start(db, r.pipeline)
        assert queued and pipeline.state == "queued" and pipeline.command_id != token


async def running(r, *, pausing=False):
    async with r.sessions() as db:
        pipeline, _ = await request_start(db, r.pipeline)
        pipeline.state = "pausing" if pausing else "generating"
        pipeline.worker_id = uuid4()
        pipeline.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
        return pipeline.command_id


@pytest.mark.asyncio
async def test_pause_is_available_while_worker_owns_project(runtime_db):
    r = runtime_db
    await running(r)
    async with project_worker_lock(r.engine, r.project) as acquired:
        assert acquired
        async with r.sessions() as db:
            pipeline = await request_pause(db, r.pipeline)
            assert pipeline.state == "pausing"
            await db.commit()


@pytest.mark.asyncio
async def test_live_worker_cannot_be_stolen_even_with_old_heartbeat(runtime_db):
    r = runtime_db
    await running(r)
    async with project_worker_lock(r.engine, r.project) as acquired:
        assert acquired
        async with project_worker_lock(r.engine, r.project) as other:
            assert not other
        async with r.sessions() as db:
            assert not await recover_abandoned(db, r.pipeline)


@pytest.mark.asyncio
@pytest.mark.parametrize("pausing,expected", [(False, "queued"), (True, "paused")])
async def test_abandoned_worker_recovery_invalidates_old_command(runtime_db, pausing, expected):
    r = runtime_db
    previous = await running(r, pausing=pausing)
    async with r.sessions() as db:
        assert await recover_abandoned(db, r.pipeline)
        await db.commit()
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        assert pipeline.state == expected and pipeline.command_id != previous
        assert pipeline.worker_id is None


@pytest.mark.asyncio
async def test_worker_lock_releases_after_exception(runtime_db):
    r = runtime_db
    with pytest.raises(RuntimeError):
        async with project_worker_lock(r.engine, r.project) as acquired:
            assert acquired
            raise RuntimeError("worker exits")
    async with r.sessions() as db:
        assert await try_command_lock(db, r.project)


async def write_generated(r):
    async with r.sessions() as db:
        pipeline, _ = await request_start(db, r.pipeline)
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text, chapter.word_count, chapter.status = "生成稿。", 4, "completed"
        chapter.summary = "生成摘要"
        status = (await db.scalars(select(PipelineChapterStatus))).one()
        status.state, status.saved_hash = "completed", content_hash(chapter.content_text)
        status.attempt_count, status.generation_task_id = 2, uuid4()
        pipeline.state, pipeline.completed_chapters, pipeline.review_round = "completed", 1, 2
        await db.flush()
        status.saved_updated_at = chapter.updated_at
        await db.commit()


@pytest.mark.asyncio
async def test_rollback_restores_body_and_all_checkpoint_state(runtime_db):
    r = runtime_db
    await write_generated(r)
    async with r.sessions() as db:
        pipeline = await request_rollback(db, r.pipeline)
        await db.commit()
        chapter = await db.get(Chapter, r.chapter)
        status = (await db.scalars(select(PipelineChapterStatus))).one()
        assert (chapter.content_text, chapter.status, chapter.summary) == ("原稿。", "draft", "原摘要")
        assert (status.state, status.attempt_count, status.saved_hash, status.generation_task_id) == ("pending", 0, None, None)
        assert pipeline.state == "planning" and pipeline.completed_chapters == pipeline.review_round == 0


@pytest.mark.asyncio
async def test_rollback_refuses_manual_edit_and_changes_nothing(runtime_db):
    r = runtime_db
    await write_generated(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = "人工精修稿。"
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(PipelineConflict, match="修改"):
            await request_rollback(db, r.pipeline)
        await db.rollback()
        chapter = await db.get(Chapter, r.chapter)
        pipeline = await db.get(PipelineRun, r.pipeline)
        assert chapter.content_text == "人工精修稿。" and pipeline.state == "completed"


@pytest.mark.asyncio
async def test_rollback_also_protects_manual_summary_changes(runtime_db):
    r = runtime_db
    await write_generated(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.summary = "作者手动修正的摘要"
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(PipelineConflict, match="修改"):
            await request_rollback(db, r.pipeline)


@pytest.mark.asyncio
async def test_rollback_rejects_partial_snapshot(runtime_db):
    r = runtime_db
    await write_generated(r)
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        pipeline.snapshot_json = {str(uuid4()): {"content_text": "错误的快照"}}
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(PipelineConflict, match="不一致"):
            await request_rollback(db, r.pipeline)


@pytest.mark.asyncio
async def test_rollback_refuses_active_writer(runtime_db):
    r = runtime_db
    await running(r)
    async with project_worker_lock(r.engine, r.project) as acquired:
        assert acquired
        async with r.sessions() as db:
            with pytest.raises(PipelineConflict, match="写入"):
                await request_rollback(db, r.pipeline)


@pytest.mark.asyncio
async def test_dispatch_observes_committed_command_and_does_not_immediately_repeat(runtime_db):
    from app.services.production.dispatch import dispatch_pending
    r = runtime_db
    async with r.sessions() as db:
        await request_start(db, r.pipeline)
        await db.commit()
    calls = []
    async def publish(pipeline_id, command_id):
        async with r.sessions() as db:
            row = await db.get(PipelineRun, pipeline_id)
            assert row.state == "queued" and str(row.command_id) == command_id
            assert row.dispatched_at is not None
        calls.append(command_id)
    assert await dispatch_pending(r.sessions, publish) == 1
    assert await dispatch_pending(r.sessions, publish) == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_broker_failure_keeps_command_and_can_retry(runtime_db):
    from app.services.production.dispatch import dispatch_pending
    r = runtime_db
    async with r.sessions() as db:
        row, _ = await request_start(db, r.pipeline)
        token = row.command_id
        await db.commit()
    async def unavailable(*args):
        raise ConnectionError("broker unavailable")
    assert await dispatch_pending(r.sessions, unavailable) == 0
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        assert row.state == "queued" and row.command_id == token and row.error_message
        row.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        await db.commit()
    calls = []
    async def available(*args): calls.append(args)
    assert await dispatch_pending(r.sessions, available) == 1
    assert calls == [(str(r.pipeline), str(token))]


@pytest.mark.asyncio
async def test_parallel_dispatchers_publish_once(runtime_db):
    from app.services.production.dispatch import dispatch_pending
    r = runtime_db
    async with r.sessions() as db:
        await request_start(db, r.pipeline)
        await db.commit()
    calls = []
    async def publish(*args): calls.append(args)
    counts = await asyncio.gather(dispatch_pending(r.sessions, publish), dispatch_pending(r.sessions, publish))
    assert sum(counts) == len(calls) == 1


async def ownership(r):
    from app.services.production.fencing import chapter_write_fence
    async with r.sessions() as db:
        row, _ = await request_start(db, r.pipeline)
        row.state = "generating"
        row.worker_id = uuid4()
        row.heartbeat_at = datetime.now(timezone.utc)
        chapter = await db.get(Chapter, r.chapter)
        version = chapter.updated_at
        await db.commit()
        return chapter_write_fence(r.pipeline, row.command_id, row.worker_id, r.chapter, version)


@pytest.mark.asyncio
async def test_fenced_write_tracks_versions_across_multiple_commits(runtime_db):
    r = runtime_db
    fence = await ownership(r)
    with fence:
        for body in ("第一稿。", "第二稿。"):
            async with r.sessions() as db:
                chapter = await db.get(Chapter, r.chapter)
                chapter.content_text = body
                await db.commit()
    async with r.sessions() as db:
        cs = (await db.scalars(select(PipelineChapterStatus))).one()
        assert cs.saved_hash == content_hash("第二稿。")


@pytest.mark.asyncio
async def test_fence_blocks_stale_worker_salvage_write(runtime_db):
    from app.services.production.fencing import StaleChapterWrite
    r = runtime_db
    fence = await ownership(r)
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        pipeline.command_id = uuid4()
        await db.commit()
    with fence:
        for body in ("旧任务最终稿。", "旧任务异常保全稿。"):
            async with r.sessions() as db:
                chapter = await db.get(Chapter, r.chapter)
                chapter.content_text = body
                with pytest.raises(StaleChapterWrite, match="失效"):
                    await db.commit()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_fence_blocks_manual_edit_conflict_even_in_fresh_session(runtime_db):
    from app.services.production.fencing import StaleChapterWrite
    r = runtime_db
    fence = await ownership(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = "人工修改。"
        await db.commit()
    with fence:
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "模型覆盖。"
            with pytest.raises(StaleChapterWrite, match="修改"):
                await db.commit()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "人工修改。"


@pytest.mark.asyncio
async def test_fence_rollback_does_not_advance_expected_version(runtime_db):
    r = runtime_db
    fence = await ownership(r)
    with fence:
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "未提交。"
            await db.flush()
            await db.rollback()
        async with r.sessions() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "正式稿。"
            await db.commit()
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "正式稿。"


@pytest.mark.asyncio
async def test_additive_migration_preserves_existing_pipeline_and_is_repeatable(runtime_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r = runtime_db
    path = Path(__file__).resolve().parents[1] / "alembic/versions/a1001921_pipeline_runtime.py"
    spec = importlib.util.spec_from_file_location("pipeline_runtime_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        row.snapshot_json = {str(r.chapter): {"content_text": "迁移前正文。"}}
        await db.commit()
    async with r.engine.begin() as conn:
        def migrate(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            # All data here is this fixture's disposable schema, not real books.
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
        await conn.run_sync(migrate)
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        assert row.snapshot_json[str(r.chapter)]["content_text"] == "迁移前正文。"
        assert row.command_id is None
        cs = (await db.scalars(select(PipelineChapterStatus))).one()
        assert cs.attempt_count == 0


@pytest.mark.asyncio
async def test_migration_creates_tables_missing_from_old_migration_history(runtime_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r = runtime_db
    path = Path(__file__).resolve().parents[1] / "alembic/versions/a1001921_pipeline_runtime.py"
    spec = importlib.util.spec_from_file_location("pipeline_fresh_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with r.engine.begin() as conn:
        # Simulates a fresh migration-only installation in the test schema.
        await conn.execute(text("DROP TABLE pipeline_chapter_statuses"))
        await conn.execute(text("DROP TABLE pipeline_runs"))
        def migrate(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.upgrade()
        await conn.run_sync(migrate)
    async with r.sessions() as db:
        pipeline = await create_pipeline(db, r.project)
        assert pipeline.total_chapters == 1
        await db.commit()


@pytest.mark.asyncio
async def test_executor_delegates_canonical_chain_and_reuses_saved_result(runtime_db, monkeypatch):
    from app.services.production.chapter_executor import execute_chapter
    from app.tasks import generation_tasks
    r = runtime_db
    await ownership(r)
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        pipeline.options_json = {"style_id": "chosen-style", "user_input": "保持有限视角", "target_words": 5000}
        cs = (await db.scalars(select(PipelineChapterStatus))).one()
        token, worker, status_id = pipeline.command_id, pipeline.worker_id, cs.id
        await db.commit()
    calls = []
    async def canonical(task_id, *, session_factory):
        assert session_factory is r.sessions
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            assert task.task_type == "chapter"
            assert task.params_json["chapter_id"] == str(r.chapter)
            assert task.params_json["style_id"] == "chosen-style"
            assert task.params_json["target_words"] == 5000
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text, chapter.status, chapter.word_count = "共享主链正文。", "completed", 7
            task.status = "completed"
            await db.commit()
        calls.append(task_id)
    monkeypatch.setattr(generation_tasks, "_run_async_generation_impl", canonical)
    first = await execute_chapter(r.sessions, r.pipeline, token, worker, status_id)
    second = await execute_chapter(r.sessions, r.pipeline, token, worker, status_id)
    assert first.state == second.state == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_executor_fences_a_superseded_canonical_generator(runtime_db, monkeypatch):
    from app.services.production.chapter_executor import execute_chapter
    from app.services.production.fencing import StaleChapterWrite
    from app.tasks import generation_tasks
    r = runtime_db
    await ownership(r)
    async with r.sessions() as db:
        pipeline = await db.get(PipelineRun, r.pipeline)
        cs = (await db.scalars(select(PipelineChapterStatus))).one()
        token, worker, status_id = pipeline.command_id, pipeline.worker_id, cs.id
    async def canonical(task_id, *, session_factory):
        async with session_factory() as db:
            pipeline = await db.get(PipelineRun, r.pipeline)
            pipeline.command_id = uuid4()
            await db.commit()
        async with session_factory() as db:
            chapter = await db.get(Chapter, r.chapter)
            chapter.content_text = "过期任务的正文。"
            await db.commit()
    monkeypatch.setattr(generation_tasks, "_run_async_generation_impl", canonical)
    with pytest.raises(StaleChapterWrite):
        await execute_chapter(r.sessions, r.pipeline, token, worker, status_id)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_rollback_consumes_snapshot_and_next_start_captures_restored_version(runtime_db):
    r = runtime_db
    await write_generated(r)
    async with r.sessions() as db:
        pipeline = await request_rollback(db, r.pipeline)
        await db.commit()
        assert pipeline.snapshot_json is None
        pipeline, created = await request_start(db, r.pipeline)
        chapter = await db.get(Chapter, r.chapter)
        assert created and chapter.content_text == "原稿。"
        assert pipeline.snapshot_json[str(r.chapter)]["updated_at"] == chapter.updated_at.isoformat()
        await db.commit()
