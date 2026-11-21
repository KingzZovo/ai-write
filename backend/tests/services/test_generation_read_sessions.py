"""Real PostgreSQL session ownership at the context/model boundary."""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.db.session import async_session_factory
from app.services import generation_context as ownership
from app.services import chapter_generator as single
from app.services import scene_orchestrator as scene
from app.services.context_pack import ContextPack


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['scene', 'single', 'stream'])
@pytest.mark.parametrize('outcome', ['success', 'error', 'cancel'])
@pytest.mark.external_services
async def test_owned_context_transaction_closed_before_model(monkeypatch, mode, outcome):
    label = 'generation-read-' + uuid.uuid4().hex
    engine = create_async_engine(settings.DATABASE_URL, connect_args={
        'server_settings': {'application_name': label},
    })
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ownership, 'async_session_factory', factory)
    entered, release = asyncio.Event(), asyncio.Event()
    sessions = []

    class Builder:
        def __init__(self, db):
            self.db = db
        async def build(self, **kwargs):
            await self.db.execute(text('SELECT 1'))
            sessions.append(self.db)
            return ContextPack(current_content='已经读出的正文。')

    async def model_boundary(**kwargs):
        assert kwargs['db'] is None  # Registry resolves routes in its own short session.
        assert not sessions[0].in_transaction()
        entered.set()
        await release.wait()
        if outcome == 'error':
            raise RuntimeError('model failed')
        return SimpleNamespace(text='生成正文。')

    async def stream_model(**kwargs):
        result = await model_boundary(**kwargs)
        yield result.text

    module = scene if mode == 'scene' else single
    monkeypatch.setattr(module, 'ContextPackBuilder', Builder)
    generator = scene.SceneOrchestrator() if mode == 'scene' else single.ChapterGenerator()
    monkeypatch.setattr(generator, '_assert_outline_chain_ready', AsyncMock())
    monkeypatch.setattr(single, '_check_character_names_in_text', AsyncMock(return_value=[]))
    if mode == 'scene':
        async def planner(**kwargs):
            await model_boundary(**kwargs)
            return []
        monkeypatch.setattr(generator, 'plan_scenes', planner)
    else:
        monkeypatch.setattr(single, 'run_text_prompt', model_boundary)
        monkeypatch.setattr(single, 'stream_text_prompt', stream_model)

    async def drive():
        args = dict(project_id=uuid.uuid4(), volume_id=uuid.uuid4(), chapter_idx=1, db=None)
        if mode == 'single':
            return await generator.generate(**args)
        stream = generator.orchestrate_chapter_stream(**args) if mode == 'scene' else generator.generate_stream(**args)
        return [chunk async for chunk in stream]

    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with async_session_factory() as monitor:
            count = await monitor.scalar(text(
                'SELECT count(*) FROM pg_stat_activity WHERE application_name=:label AND xact_start IS NOT NULL'
            ), {'label': label})
            assert count == 0
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == 'error':
                with pytest.raises(RuntimeError, match='model failed'):
                    await task
            else:
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.external_services
async def test_borrowed_session_keeps_flushed_and_pending_author_changes():
    from app.models.project import Project
    async with async_session_factory() as db:
        project = Project(title='未提交的作者修改')
        db.add(project)
        await db.flush()
        pid = project.id
        project.title = '尚未 flush 的修改'
        async with ownership.generation_context_session(db) as context_db:
            assert context_db is db
        assert db.in_transaction()
        assert project in db.dirty
        assert project.title == '尚未 flush 的修改'
        assert await db.get(Project, pid) is project
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.external_services
async def test_cancel_during_owned_context_read_releases_connection(monkeypatch):
    label = 'generation-cancel-' + uuid.uuid4().hex
    engine = create_async_engine(settings.DATABASE_URL, connect_args={
        'server_settings': {'application_name': label},
    })
    monkeypatch.setattr(ownership, 'async_session_factory', async_sessionmaker(engine))
    ready = asyncio.Event()
    async def read():
        async with ownership.generation_context_session(None) as db:
            await db.execute(text('SELECT 1'))
            ready.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with async_session_factory() as monitor:
            assert await monitor.scalar(text(
                'SELECT count(*) FROM pg_stat_activity WHERE application_name=:label AND xact_start IS NOT NULL'
            ), {'label': label}) == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.external_services
async def test_quality_rewrite_owns_short_genre_and_roster_reads(monkeypatch):
    from sqlalchemy import delete
    from app.models.project import Project
    from app.services import chapter_quality_gate as gate
    label = 'quality-read-' + uuid.uuid4().hex
    engine = create_async_engine(settings.DATABASE_URL, connect_args={
        'server_settings': {'application_name': label},
    })
    monkeypatch.setattr(ownership, 'async_session_factory', async_sessionmaker(engine))
    async with async_session_factory() as db:
        project = Project(title='quality-session-regression', genre='现实生活')
        db.add(project)
        await db.commit()
        pid = project.id
    calls = []
    prose = '沈青合上本子，把钥匙递给陈川。窗外的雨停了。'
    async def rewrite(**kwargs):
        assert kwargs['db'] is None
        async with async_session_factory() as monitor:
            assert await monitor.scalar(text(
                'SELECT count(*) FROM pg_stat_activity WHERE application_name=:label AND xact_start IS NOT NULL'
            ), {'label': label}) == 0
        calls.append(kwargs)
        return SimpleNamespace(text=prose)
    monkeypatch.setattr(gate, 'run_text_prompt', rewrite)
    try:
        assert await gate._resolve_project_genre_hint(None, str(pid)) == '现实生活'
        result = await gate.apply_chapter_quality_gate(
            text='[CH-12]\n' + prose, db=None, project_id=str(pid), max_rewrite_rounds=1,
        )
        assert len(calls) == 1
        assert result.final_text == prose
        assert result.status == 'passed'
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Project).where(Project.id == pid))
            await db.commit()
        await engine.dispose()
