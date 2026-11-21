"""HTTP -> durable command -> task adapter, using independent DB connections."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from tests.test_pipeline_runtime import canonical
from app.api import pipeline as api
from app.db import session
from app.models.pipeline import PipelineRun
from app.models.project import Chapter
from app.services.production import dispatch
from app.tasks import generation_tasks, celery_app


@pytest_asyncio.fixture
async def client(runtime_db, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    async def db_dependency():
        async with runtime_db.sessions() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    app.dependency_overrides[session.get_db] = db_dependency
    monkeypatch.setattr(session, "async_session_factory", runtime_db.sessions)
    monkeypatch.setattr(session._state, "engine", runtime_db.engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_http_start_commits_before_delivery_and_duplicate_is_idempotent(runtime_db, client, monkeypatch):
    r = runtime_db
    calls = []
    async def publish(pipeline_id, token):
        async with r.sessions() as db:
            row = await db.get(PipelineRun, pipeline_id)
            assert row.state == "queued" and str(row.command_id) == token
            assert row.dispatched_at is not None and row.snapshot_json
        calls.append((pipeline_id, token))
    monkeypatch.setattr(dispatch, "publish_command", publish)
    for _ in range(2):
        response = await client.post(f"/api/pipeline/{r.pipeline}/start")
        assert response.status_code == 200 and response.json()["state"] == "queued"
    assert len(calls) == 1
    model_calls = canonical(monkeypatch)
    assert await generation_tasks._run_pipeline_async(*calls[0])
    response = await client.get(f"/api/pipeline/{r.pipeline}")
    assert response.json()["state"] == "completed" and len(model_calls) == 1
    assert response.json()["chapters"][0]["generation_task_id"]


@pytest.mark.asyncio
async def test_broker_failure_leaves_durable_queue_and_periodic_retry(runtime_db, client, monkeypatch):
    r = runtime_db
    async def unavailable(*args):
        raise ConnectionError("broker down")
    monkeypatch.setattr(dispatch, "publish_command", unavailable)
    assert (await client.post(f"/api/pipeline/{r.pipeline}/start")).status_code == 200
    status = (await client.get(f"/api/pipeline/{r.pipeline}")).json()
    assert status["state"] == "queued" and "自动重试" in status["error_message"]
    async with r.sessions() as db:
        row = await db.get(PipelineRun, r.pipeline)
        token = row.command_id
        row.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        await db.commit()
    calls = []
    async def publish(pipeline_id, command_id):
        calls.append((pipeline_id, command_id))
    monkeypatch.setattr(dispatch, "publish_command", publish)
    assert await dispatch.dispatch_queued_commands() == 1
    assert calls == [(str(r.pipeline), str(token))]
    assert celery_app.conf.beat_schedule["pipeline-command-recovery"]["task"] in celery_app.tasks
    assert celery_app.conf.task_routes["tasks.dispatch_pipeline_commands"]["queue"] == "pipeline-control"


@pytest.mark.asyncio
async def test_pause_invalidation_and_rollback_preserve_original(runtime_db, client, monkeypatch):
    r = runtime_db
    calls = []
    async def publish(*args):
        calls.append(args)
    monkeypatch.setattr(dispatch, "publish_command", publish)
    await client.post(f"/api/pipeline/{r.pipeline}/start")
    response = await client.post(f"/api/pipeline/{r.pipeline}/rollback")
    assert response.status_code == 409
    assert (await client.post(f"/api/pipeline/{r.pipeline}/pause")).json()["state"] == "paused"
    assert not await generation_tasks._run_pipeline_async(*calls[0])
    response = await client.post(f"/api/pipeline/{r.pipeline}/rollback")
    assert response.status_code == 200 and response.json()["state"] == "planning"
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == "原稿。"


@pytest.mark.asyncio
async def test_pause_during_real_runtime_reaches_chapter_boundary(runtime_db, client, monkeypatch):
    r = runtime_db
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def publish(*args):
        calls.append(args)
    async def hold(*args):
        entered.set()
        await release.wait()
    monkeypatch.setattr(dispatch, "publish_command", publish)
    canonical(monkeypatch, on_call=hold)
    await client.post(f"/api/pipeline/{r.pipeline}/start")
    worker = asyncio.create_task(generation_tasks._run_pipeline_async(*calls[0]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        response = await client.post(f"/api/pipeline/{r.pipeline}/pause")
        assert response.json()["state"] == "pausing"
        assert response.json()["current_chapter_id"] == str(r.chapter)
    finally:
        release.set()
        await asyncio.wait_for(worker, 3)
    assert (await client.get(f"/api/pipeline/{r.pipeline}")).json()["state"] == "paused"


@pytest.mark.asyncio
async def test_create_validates_options_and_forbids_runtime_identifiers(runtime_db, client):
    for options in ({"max_attempts": 0}, {"target_words": 10}, {"pipeline_id": str(uuid4())}):
        response = await client.post("/api/pipeline", json={"project_id": str(runtime_db.project), "options": options})
        assert response.status_code == 422
    response = await client.post("/api/pipeline", json={
        "project_id": str(runtime_db.project), "options": {"target_words": 5000, "user_input": "有限视角"},
    })
    assert response.status_code == 201
    assert response.json()["options"]["user_input"] == "有限视角"


@pytest.mark.asyncio
async def test_missing_commands_return_404(client):
    for command in ("start", "pause", "rollback"):
        assert (await client.post(f"/api/pipeline/{uuid4()}/{command}")).status_code == 404


@pytest.mark.asyncio
async def test_tokenless_legacy_message_cannot_generate(runtime_db, monkeypatch):
    calls = canonical(monkeypatch)
    assert not await generation_tasks._run_pipeline_async(str(runtime_db.pipeline))
    assert not calls


@pytest.mark.asyncio
async def test_publisher_passes_both_tokens_without_broker_retry(monkeypatch):
    from unittest.mock import AsyncMock

    sent = []
    def apply_async(**kwargs):
        sent.append(kwargs)
    # This unit case verifies the offload boundary and broker arguments.
    # Actual threads and broker delivery are covered by test_pipeline_processes.
    async def offload(function, *args, **kwargs):
        return function(*args, **kwargs)
    thread_boundary = AsyncMock(side_effect=offload)
    monkeypatch.setattr(dispatch.asyncio, "to_thread", thread_boundary)
    monkeypatch.setattr(generation_tasks.run_pipeline_generation, "apply_async", apply_async)
    await dispatch.publish_command("pipeline", "command")
    assert sent == [{"args": ["pipeline", "command"], "retry": False}]
    thread_boundary.assert_awaited_once_with(
        apply_async, args=["pipeline", "command"], retry=False,
    )


@pytest.mark.asyncio
async def test_history_limit_and_safe_ui_metadata(runtime_db, client):
    r = runtime_db
    created = await client.post("/api/pipeline", json={"project_id": str(r.project)})
    assert created.status_code == 201
    response = await client.get(f"/api/pipeline/project/{r.project}?limit=1")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1 and rows[0]["id"] == created.json()["id"]
    assert rows[0]["created_at"] and rows[0]["volume_id"] is None
    assert rows[0]["can_rollback"] is False and "snapshot_json" not in rows[0]
    assert (await client.get(f"/api/pipeline/project/{r.project}?limit=0")).status_code == 422
