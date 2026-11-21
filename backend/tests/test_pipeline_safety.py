"""Project/prose regressions in explicit disposable PostgreSQL schemas.

Worker tests exercise the durable runtime with a canonical generator stand-in;
no model/network calls or application databases are used.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, delete

from app.models.pipeline import PipelineChapterStatus, PipelineRun
from app.models.project import Chapter, Project, Volume
from app.services import pipeline_service
from app.tasks import generation_tasks


from tests.test_pipeline_control import runtime_db
from app.models.generation_task import GenerationTask
from app.services.production.control import request_start


@pytest_asyncio.fixture
async def sessions(runtime_db):
    # Remove only fixture seed rows inside its random disposable schema.
    async with runtime_db.sessions() as db:
        for model in (PipelineChapterStatus, PipelineRun, Chapter, Volume, Project):
            await db.execute(delete(model))
        await db.commit()
    yield runtime_db.sessions


async def book(db, title="测试书", volumes=1, chapters=1):
    project = Project(title=title)
    db.add(project)
    await db.flush()
    rows = []
    for volume_idx in range(1, volumes + 1):
        volume = Volume(project_id=project.id, volume_idx=volume_idx, title=f"卷{volume_idx}")
        db.add(volume)
        await db.flush()
        for chapter_idx in range(1, chapters + 1):
            ch = Chapter(
                volume_id=volume.id, chapter_idx=chapter_idx,
                title=f"{title}-卷{volume_idx}-章{chapter_idx}",
                content_text="已保存的正文。", word_count=7, status="completed",
            )
            db.add(ch)
            rows.append(ch)
        await db.flush()
    return project, rows


@pytest.mark.asyncio
async def test_empty_project_never_captures_other_books(sessions):
    async with sessions() as db:
        other, chapters = await book(db)
        empty, _ = await book(db, title="空项目", volumes=0)
        with pytest.raises(ValueError, match="章节|大纲"):
            await pipeline_service.create_pipeline(db, empty.id)
        assert not (await db.execute(select(PipelineRun))).scalars().all()
        assert chapters[0].content_text == "已保存的正文。"


@pytest.mark.asyncio
async def test_cross_project_volume_rejected(sessions):
    async with sessions() as db:
        owner, chapters = await book(db)
        other, _ = await book(db, title="另一本书", volumes=0)
        with pytest.raises(ValueError, match="卷|项目"):
            await pipeline_service.create_pipeline(db, other.id, chapters[0].volume_id)


@pytest.mark.asyncio
async def test_owned_volume_selects_only_that_volume(sessions):
    async with sessions() as db:
        project, chapters = await book(db, volumes=2, chapters=2)
        pipeline = await pipeline_service.create_pipeline(db, project.id, chapters[2].volume_id)
        status = await pipeline_service.get_pipeline_status(db, pipeline.id)
        assert [row["chapter_id"] for row in status["chapters"]] == [str(ch.id) for ch in chapters[2:]]


@pytest.mark.asyncio
async def test_deleted_or_missing_project_rejected(sessions):
    from datetime import datetime, timezone
    async with sessions() as db:
        project, _ = await book(db)
        project.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        for project_id in (project.id, uuid.uuid4()):
            with pytest.raises(ValueError, match="项目"):
                await pipeline_service.create_pipeline(db, project_id)


@pytest.mark.asyncio
async def test_status_identifies_and_orders_volume_local_chapters(sessions):
    async with sessions() as db:
        project, chapters = await book(db, volumes=2, chapters=2)
        pipeline = await pipeline_service.create_pipeline(db, project.id)
        status = await pipeline_service.get_pipeline_status(db, pipeline.id)
        assert [r["chapter_id"] for r in status["chapters"]] == [str(c.id) for c in chapters]
        assert [r["volume_idx"] for r in status["chapters"]] == [1, 1, 2, 2]
        assert [r["chapter_idx"] for r in status["chapters"]] == [1, 2, 1, 2]


@pytest.mark.parametrize("output", ["", " \n\t", "[CH-1]"])
def test_empty_cleaned_output_cannot_overwrite_saved_prose(output):
    chapter = SimpleNamespace(id="c", content_text="原有正文。", word_count=5, status="completed")
    before = vars(chapter).copy()
    with pytest.raises(ValueError, match="正文|空"):
        generation_tasks._persist_pipeline_chapter_text(chapter, output)
    assert vars(chapter) == before


async def worker_fixture(sessions, monkeypatch, output, volumes=1, chapters=1):
    from app.db import session
    from app.services import entity_dispatch, model_router
    async with sessions() as db:
        project, rows = await book(db, volumes=volumes, chapters=chapters)
        pipeline = await pipeline_service.create_pipeline(db, project.id)
        pipeline.options_json = {"max_attempts": 1}
        await request_start(db, pipeline.id)
        command_id = pipeline.command_id
        pipeline_id = pipeline.id
        titles = [row.title for row in rows]
        await db.commit()
    outputs = output if isinstance(output, list) else [output] * len(titles)
    router = SimpleNamespace(generate=AsyncMock(side_effect=[SimpleNamespace(text=text) for text in outputs]))
    dispatch = AsyncMock()
    monkeypatch.setattr(session, "async_session_factory", sessions)
    monkeypatch.setattr(session._state, "engine", sessions.kw["bind"])
    async def canonical(task_id, *, session_factory):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            chapter = await db.get(Chapter, task.params_json["chapter_id"])
            output = await router.generate(messages=[{}, {"content": chapter.title}])
            flagged = generation_tasks._persist_pipeline_chapter_text(chapter, output.text)
            task.status = "needs_review" if flagged else "completed"
            await db.commit()
            if not flagged:
                await dispatch()
    monkeypatch.setattr(generation_tasks, "_run_async_generation_impl", canonical)
    await generation_tasks._run_pipeline_async(str(pipeline_id), str(command_id))
    return pipeline_id, titles, router, dispatch


@pytest.mark.asyncio
async def test_worker_generates_in_volume_then_chapter_order(sessions, monkeypatch):
    _, titles, router, _ = await worker_fixture(sessions, monkeypatch, "新的完整正文。", volumes=2, chapters=2)
    prompts = [call.kwargs["messages"][1]["content"] for call in router.generate.call_args_list]
    assert len(prompts) == len(titles)
    assert all(title in prompt for title, prompt in zip(titles, prompts))


@pytest.mark.asyncio
async def test_worker_empty_response_preserves_original_and_records_failure(sessions, monkeypatch):
    pipeline_id, _, _, dispatch = await worker_fixture(sessions, monkeypatch, "")
    async with sessions() as db:
        chapter = (await db.execute(select(Chapter))).scalar_one()
        cs = (await db.execute(select(PipelineChapterStatus))).scalar_one()
        pipeline = await db.get(PipelineRun, pipeline_id)
        assert chapter.content_text == "已保存的正文。"
        assert chapter.word_count == 7 and chapter.status == "completed"
        assert cs.state == "failed" and cs.error_message
        assert pipeline.completed_chapters == 0
        dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewable_chapter_does_not_block_later_chapters(sessions, monkeypatch):
    pipeline_id, _, router, dispatch = await worker_fixture(
        sessions, monkeypatch, ["他推开门却看见", "这一章的故事已经结束。"], chapters=2,
    )
    async with sessions() as db:
        status = await pipeline_service.get_pipeline_status(db, pipeline_id)
        assert [row["state"] for row in status["chapters"]] == ["needs_review", "completed"]
        assert status["completed_chapters"] == 1 and status["needs_review_chapters"] == 1
        assert status["state"] == "needs_review"
        assert router.generate.await_count == 2 and dispatch.await_count == 1


@pytest.mark.asyncio
async def test_explicit_retry_reopens_only_reviewable_chapters(sessions, monkeypatch):
    pipeline_id, _, _, _ = await worker_fixture(
        sessions, monkeypatch, ["他推开门却看见", "这一章的故事已经结束。"], chapters=2,
    )
    async with sessions() as db:
        await request_start(db, pipeline_id)
        status = await pipeline_service.get_pipeline_status(db, pipeline_id)
        assert status["state"] == "queued"
        assert [row["state"] for row in status["chapters"]] == ["pending", "completed"]
        assert status["completed_chapters"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_scope", ["project", "volume"])
async def test_worker_rejects_legacy_out_of_scope_entries(sessions, monkeypatch, wrong_scope):
    from app.db import session
    from app.services import model_router
    async with sessions() as db:
        owner, chapters = await book(db, volumes=2)
        other, _ = await book(db, title="另一本", volumes=0)
        pipeline = PipelineRun(
            project_id=other.id if wrong_scope == "project" else owner.id,
            volume_id=None if wrong_scope == "project" else chapters[1].volume_id,
            state="queued", command_id=uuid.uuid4(), total_chapters=1,
        )
        db.add(pipeline)
        await db.flush()
        cs = PipelineChapterStatus(pipeline_id=pipeline.id, chapter_id=chapters[0].id, chapter_idx=1)
        db.add(cs)
        pipeline_id = pipeline.id
        chapter_id = chapters[0].id
        command_id = pipeline.command_id
        await db.commit()
    router = SimpleNamespace(generate=AsyncMock())
    monkeypatch.setattr(session, "async_session_factory", sessions)
    monkeypatch.setattr(model_router, "get_model_router_async", AsyncMock(return_value=router))
    monkeypatch.setattr(session._state, "engine", sessions.kw["bind"])
    await generation_tasks._run_pipeline_async(str(pipeline_id), str(command_id))
    async with sessions() as db:
        chapter = await db.get(Chapter, chapter_id)
        status = await pipeline_service.get_pipeline_status(db, pipeline_id)
        assert chapter.content_text == "已保存的正文。"
        assert status["chapters"][0]["state"] == "failed"
        assert "所属" in status["chapters"][0]["error_message"]
        router.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_create_enforces_scope_and_returns_stable_chapter_ids(sessions):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.pipeline import router
    from app.db.session import get_db
    async with sessions() as db:
        project, chapters = await book(db)
        empty, _ = await book(db, title="空项目", volumes=0)
        await db.commit()
    app = FastAPI()
    app.include_router(router)
    async def isolated_db():
        async with sessions() as db:
            yield db
            await db.commit()
    app.dependency_overrides[get_db] = isolated_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/pipeline", json={"project_id": str(empty.id)})
        assert response.status_code == 400
        response = await client.post("/api/pipeline", json={"project_id": str(empty.id), "volume_id": str(chapters[0].volume_id)})
        assert response.status_code == 400
        response = await client.post("/api/pipeline", json={"project_id": str(project.id)})
        assert response.status_code == 201
        assert response.json()["chapters"][0]["chapter_id"] == str(chapters[0].id)


@pytest.mark.asyncio
async def test_worker_truncated_draft_is_reviewable_not_completed(sessions, monkeypatch):
    pipeline_id, _, _, dispatch = await worker_fixture(sessions, monkeypatch, "他推开门却看见")
    async with sessions() as db:
        chapter = (await db.execute(select(Chapter))).scalar_one()
        cs = (await db.execute(select(PipelineChapterStatus))).scalar_one()
        pipeline = await db.get(PipelineRun, pipeline_id)
        assert chapter.status == "draft" and chapter.content_text == "他推开门却看见"
        assert cs.state == "needs_review" and cs.error_message
        assert pipeline.completed_chapters == 0 and pipeline.state == "needs_review"
        dispatch.assert_not_awaited()
