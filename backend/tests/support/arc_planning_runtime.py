"""Actual SQLite transactions with the shared physical-connection test adapter."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import re
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.api import arc, arc_planning as api
from app.db.session import get_db
from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.cognition import ChapterCognition, CognitionRevision, CognitionRequest
from app.models.arc_progress import ArcProgressWrite
from app.models.pipeline import PipelineRun, PipelineChapterStatus
from app.models.project import Chapter, Outline, Project, Volume, WorldRule
from app.services import arc_planning_execution as execution, chapter_order, generation_task_execution, prompt_registry
from app.services.generation_execution import GenerationExecutionUnavailable
from tests.support.arc_outline import arc_meta, arc_chapter
from tests.support.independent_task_runtime import install_runtime
from tests.test_arc_planning_journal import route
from tests.test_generation_execution import LocalSession
from tests.support.world_rules import RuleGraph
from tests.support.character_graph import CharacterGraph


def snapshot(r, request_id):
    with Session(r.engine) as db:
        row = db.get(Request, UUID(str(request_id)))
        return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns} if row else None


def volume_count(r):
    with Session(r.engine) as db:
        return db.scalar(sa.select(sa.func.count()).select_from(Volume).where(Volume.project_id == r.pid))


@pytest.fixture
def planning_book(tmp_path, monkeypatch):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "arc-planning.sqlite"}')
    @sa.event.listens_for(engine, "connect")
    def keys(connection, _record): connection.execute("PRAGMA foreign_keys=ON")
    for model in (Project, Volume, Chapter, Outline, WorldRule, PipelineRun, PipelineChapterStatus, ArcProgressWrite, Request,
                  ChapterCognition, CognitionRevision, CognitionRequest):
        model.__table__.create(engine)
    pid, foreign, vid, cid = [uuid4() for _ in range(4)]
    with engine.begin() as db:
        db.execute(sa.insert(Project), [{"id": pid, "title": "弧规划恢复", "target_word_count": 5_000_000},
            {"id": foreign, "title": "另一部书", "target_word_count": 5_000_000}])
        db.execute(sa.insert(Volume).values(id=vid, project_id=pid, title="前面的普通分卷", volume_idx=5))
        db.execute(sa.insert(Chapter).values(id=cid, volume_id=vid, chapter_idx=1, global_idx=1,
            title="前章", content_text="既有作者正文。", status="draft"))
    r = NS(engine=engine, pid=pid, foreign=foreign, vid=vid, cid=cid, published=[], calls=[], queries=[],
           before_call=None, http_fault=None, worker_fault=None, route_calls=[])
    r.graph = RuleGraph(pid)
    r.character_graph = CharacterGraph(pid)
    async def character_driver(): yield r.character_graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', character_driver)
    async def rule_driver(): return r.graph
    monkeypatch.setattr('app.services.world_rule_sources.current_rule_driver', rule_driver)
    @sa.event.listens_for(engine, "before_execute")
    def capture(conn, statement, multiparams, params, execution_options): r.queries.append(str(statement))
    runtime = install_runtime(monkeypatch, engine)
    r.runtime = runtime
    async def scalars(db, statement): return db.session.scalars(statement)
    monkeypatch.setattr(runtime.OwnedSession, "scalars", scalars, raising=False)
    monkeypatch.setattr(runtime.OwnedSession, "in_transaction", lambda db: db.session.in_transaction(), raising=False)
    monkeypatch.setattr(execution, "generation_execution_session", generation_task_execution.generation_execution_session)
    async def command_lock(db, project_id):
        return UUID(str(project_id)) not in runtime.held or getattr(getattr(db, "guard", None), "project_id", None) == str(project_id)
    monkeypatch.setattr(chapter_order, "try_command_lock", command_lock)
    monkeypatch.setattr(chapter_order, "require_materialization_lock", AsyncMock())
    class HttpSession(LocalSession):
        async def scalars(self, statement): return self.session.scalars(statement)
        async def commit(self):
            fault, r.http_fault = r.http_fault, None
            if fault == "before": raise RuntimeError("private commit failure")
            await super().commit()
            if fault == "after": raise RuntimeError("private acknowledgement failure")
    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as session:
            yield HttpSession(session)
    r.sessions = sessions
    original_commit = runtime.OwnedSession.commit
    async def worker_commit(db):
        fault = r.worker_fault
        request = next((row for row in db.session.identity_map.values() if isinstance(row, Request)), None)
        trigger = fault and request is not None and fault["condition"](request)
        if trigger:
            r.worker_fault = None
            if fault["when"] == "after": await original_commit(db)
            db.guard.revoke()
            if fault.get("cancel"): raise asyncio.CancelledError()
            raise GenerationExecutionUnavailable()
        await original_commit(db)
    monkeypatch.setattr(runtime.OwnedSession, "commit", worker_commit)
    async def database():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    async def publish(identity, *, manual=False):
        assert snapshot(r, identity) is not None, "publish preceded committed submission"
        r.published.append((identity, manual))
    monkeypatch.setattr(api, "publish_arc_request", publish)
    async def capture_route(task, db):
        r.route_calls.append(task)
        return route(task)
    monkeypatch.setattr(execution, "capture_prompt_snapshot", capture_route)
    async def model(task, user, db, extra="", **kwargs):
        assert not db.bind.in_transaction(), "model call held a database transaction"
        with Session(engine) as reader:
            stored = reader.scalar(sa.select(Request).where(Request.project_id == pid, Request.status == "running"))
            assert stored is not None
            attempt = stored.journal_json["stages"][-1]["attempts"][-1]
            assert attempt["state"] == "reserved" and attempt["input"] == user
            assert stored.journal_json["stages"][-1]["route"] == kwargs["frozen_prompt"]
        r.calls.append((task, user, deepcopy(kwargs)))
        if r.before_call is not None:
            result = await r.before_call(task, user, db, kwargs)
            if result is not None:
                return result
        target = int(re.search(r"本弧准确规划 (\d+) 章", user).group(1))
        if task == "arc_outline_meta":
            output = arc_meta(target)
        else:
            start, end = map(int, re.search(r"start=(\d+), end=(\d+)", user).groups())
            output = {"chapter_summaries": [arc_chapter(i) for i in range(start, end + 1)]}
        return NS(text=json.dumps(output, ensure_ascii=False), finish_reason="stop")
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    app = FastAPI(); app.include_router(api.router); app.include_router(arc.router)
    app.dependency_overrides[get_db] = database
    r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield r
    engine.dispose()


def path(r, suffix=""):
    return f"/api/projects/{r.pid}/arc-plans{suffix}"


async def request_body(r, *, target=11, **values):
    async with r.client() as client:
        source = await client.get(path(r, "/source"))
    assert source.status_code == 200, source.text
    return dict(request_id=str(uuid4()), expected_source=source.json()["source"], mode=source.json()["mode"], **{
        "idea": "还钱以后", "background": "欠三千，交两千，余一千。", "core_setup": "姐弟据实际进项商量还款。",
        "opening_scene": "信封放在碗下面。", "target_chapters": target, **values})


async def submit(r, body):
    async with r.client() as client:
        return await client.post(path(r), json=body)


async def run(r, identity, *, manual=False):
    return await execution.run_arc_request(identity, manual=manual, sessions=r.sessions)
