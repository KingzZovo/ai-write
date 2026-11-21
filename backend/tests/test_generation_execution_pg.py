"""Real worker exclusion, transaction ownership and lost PG-session fencing.

Never run against an application database. The imported public fixture only
permits a disposable local test database and creates its own random schema.
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.db.session import Base
from app.models.generation_run import CriticReport, GenerationRun, GenerationRunSubmission
from app.models.project import Chapter, ChapterVariant, Project, Volume
from app.services import generation_runner as runner
from app.services.generation_execution import GenerationExecutionUnavailable, generation_execution_session
from app.services.production.control import PipelineConflict, request_start
from app.services.production.locking import try_command_lock
from app.services.review_contract import make_critic_report
from app.services.generation_run_submissions import make_run_submission, run_submission_intent
from tests.test_pipeline_control import runtime_db
from tests.support.generation_run_fixture import verified_history

pytestmark = pytest.mark.external_services
DRAFT = '沈青数出两千元，等弟弟伸手来接。'


async def setup_runner(r, monkeypatch):
    async with r.engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            GenerationRun.__table__, GenerationRunSubmission.__table__, CriticReport.__table__, ChapterVariant.__table__,
        ]))
    async def plan(_run, db, **_kwargs):
        await db.commit()
        return {'pack': {'system_prompt': '当前核实的前文'}}
    async def review(**kwargs):
        return make_critic_report(kwargs['draft'], issues=[], stages=[{'name': 'critic', 'status': 'complete'}])
    monkeypatch.setattr(runner, '_phase_planning', plan)
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(side_effect=review))
    monkeypatch.setattr(runner.run_bus, 'publish', AsyncMock())
    monkeypatch.setenv('BVSR_ENABLED', 'false')
    monkeypatch.setenv('AGENT_TOOL_LOOP_ENABLED', 'false')


@pytest.mark.asyncio
async def test_legacy_execution_excludes_start_resume_and_production_commands(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def draft(_run, db, _planning):
        # Physical ownership persists while no business transaction is open.
        assert not db.in_transaction()
        entered.set()
        await release.wait()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    drafting = AsyncMock(side_effect=draft)
    monkeypatch.setattr(runner, '_phase_drafting', drafting)
    async def start():
        async with r.sessions() as caller:
            chapter = await caller.get(Chapter, r.chapter)
            return await runner.start_run(project_id=str(r.project), chapter_id=str(r.chapter), db=caller,
                max_rewrite_count=0, request_id=uuid4(), expected_content_text=chapter.content_text or '')
    first = asyncio.create_task(start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with r.sessions() as observer:
            rid = await observer.scalar(select(GenerationRun.id).where(GenerationRun.project_id == r.project))
            await observer.rollback()
            with pytest.raises(PipelineConflict): await start()
            with pytest.raises(PipelineConflict): await runner.execute_run(run_id=str(rid), db=observer, resume=True)
            with pytest.raises(PipelineConflict): await request_start(observer, r.pipeline)
            await observer.rollback()
            assert await observer.scalar(select(func.count()).select_from(GenerationRun)) == 1
        release.set()
        result = await first
        assert result['status'] == 'done' and result['final_text'] == DRAFT
        drafting.assert_awaited_once()
        async with r.sessions() as observer:
            assert await observer.scalar(select(func.count()).select_from(CriticReport)) == 1
            assert await try_command_lock(observer, r.project)
            await observer.rollback()
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('late_outcome', ['delivered_report', 'model_error'])
async def test_terminated_old_actor_cannot_overwrite_new_checkpoint(runtime_db, monkeypatch, late_outcome):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    async with r.sessions() as seed:
        row = GenerationRun(id=uuid4(), project_id=r.project, chapter_id=r.chapter,
                            phase='critic', status='running', max_rewrite_count=0,
                            checkpoint_data={'drafting': {'text': DRAFT, 'delivery_status': 'complete'}})
        seed.add_all(verified_history(row))
        seed.add(row)
        chapter = await seed.get(Chapter, r.chapter)
        volume = await seed.get(Volume, chapter.volume_id)
        intent = run_submission_intent(request_id=row.id, project_id=r.project, chapter_id=r.chapter,
            max_rewrite_count=0, expected_content_text=chapter.content_text or '')
        seed.add(make_run_submission(intent, chapter, volume))
        await seed.commit()
        rid = row.id
    entered, release = asyncio.Event(), asyncio.Event()
    owner = {}
    calls = 0
    async def review(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            db = kwargs['db']
            owner['pid'] = await db.scalar(text('SELECT pg_backend_pid()'))
            await db.commit()
            entered.set()
            await release.wait()
            if late_outcome == 'model_error': raise RuntimeError('late model failure')
        return make_critic_report(kwargs['draft'], issues=[], stages=[{'name': 'critic', 'status': 'complete'}])
    monkeypatch.setattr(runner, 'run_critic', review)
    async def execute():
        async with r.sessions() as caller:
            return await runner.execute_run(run_id=str(rid), db=caller, resume=True)
    old = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with r.sessions() as observer:
            assert owner['pid'] != await observer.scalar(text('SELECT pg_backend_pid()'))
            # The PID belongs only to the test-created, currently blocked
            # legacy actor in this fixture's disposable database.
            assert await observer.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': owner['pid']})
            await observer.commit()
            async with asyncio.timeout(5):
                while await observer.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE pid=:pid'), {'pid': owner['pid']}):
                    await observer.rollback()
                    await asyncio.sleep(0.01)
            await observer.rollback()
        current = await execute()
        assert current['status'] == 'done'
        release.set()
        with pytest.raises(GenerationExecutionUnavailable): await old
        async with r.sessions() as observer:
            stored = await observer.get(GenerationRun, rid)
            assert stored.status == 'done' and stored.checkpoint_data == current['checkpoint_data']
            assert 'failure' not in stored.checkpoint_data
            assert await observer.scalar(select(func.count()).select_from(CriticReport)) == 1
    finally:
        release.set()
        if not old.done(): old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_execution_releases_its_session_lock(runtime_db):
    r = runtime_db
    entered = asyncio.Event()
    async def hold():
        async with r.sessions() as caller:
            async with generation_execution_session(caller, r.project) as db:
                await db.execute(text('SELECT 1'))
                await db.commit()
                entered.set()
                await asyncio.Event().wait()
    task = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        async with r.sessions() as observer:
            assert await try_command_lock(observer, r.project)
            await observer.rollback()
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_execution_never_commits_the_callers_pending_work(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    monkeypatch.setattr(runner, '_phase_drafting', AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'}))
    async with r.sessions() as caller:
        project = await caller.get(Project, r.project)
        previous = project.title
        project.title = '调用者尚未提交的改名'
        await caller.flush()
        chapter = await caller.get(Chapter, r.chapter)
        result = await runner.start_run(project_id=str(r.project), chapter_id=str(r.chapter), db=caller,
            max_rewrite_count=0, request_id=uuid4(), expected_content_text=chapter.content_text or '')
        assert result['status'] == 'done'
        assert caller.in_transaction()
        await caller.rollback()
    async with r.sessions() as reader:
        assert (await reader.get(Project, r.project)).title == previous
        assert await reader.scalar(select(func.count()).select_from(GenerationRun)) == 1
