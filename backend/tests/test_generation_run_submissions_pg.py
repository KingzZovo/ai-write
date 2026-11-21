"""Real PostgreSQL acceptance, source locks and retained receipt checks.

These use only the existing disposable-schema fixture. They are explicitly
external-services tests and are not executed by the local offline run.
"""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select

from app.models.generation_run import GenerationRun, GenerationRunSubmission
from app.models.project import Chapter, Project, Volume
from app.services import generation_run_submissions as submissions, generation_runner as runner
from app.services.generation_execution import GenerationExecutionSession, GenerationExecutionUnavailable
from app.services.production.control import PipelineConflict
from tests.test_generation_execution_pg import DRAFT, setup_runner
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def run_book(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    r.draft = AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'})
    monkeypatch.setattr(runner, '_phase_drafting', r.draft)
    r.request = uuid4()
    async with r.sessions() as reader:
        r.original = (await reader.get(Chapter, r.chapter)).content_text or ''
    return r


async def start(r, **values):
    async with r.sessions() as caller:
        return await runner.start_run(**{
            'project_id': r.project, 'chapter_id': r.chapter, 'db': caller,
            'request_id': r.request, 'expected_content_text': r.original,
            'max_rewrite_count': 0, **values,
        })


@pytest.mark.asyncio
async def test_committed_running_request_is_observed_concurrently_without_more_execution(run_book):
    r = run_book
    entered, release = asyncio.Event(), asyncio.Event()
    async def draft(_run, db, _plan):
        assert not db.in_transaction()
        entered.set()
        await release.wait()
        return {'text': DRAFT, 'delivery_status': 'complete'}
    r.draft.side_effect = draft
    first = asyncio.create_task(start(r))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        observed = await asyncio.wait_for(asyncio.gather(*(start(r) for _ in range(4))), 5)
        assert all(row['id'] == r.request and row['status'] == 'running' for row in observed)
        with pytest.raises(PipelineConflict): await start(r, request_id=uuid4())
        with pytest.raises(submissions.RunSubmissionError) as conflict:
            await start(r, max_rewrite_count=1)
        assert conflict.value.detail['code'] == 'generation_request_conflict'
        async with r.sessions() as reader:
            assert await reader.scalar(select(func.count()).select_from(GenerationRun)) == 1
            assert await reader.scalar(select(func.count()).select_from(GenerationRunSubmission)) == 1
        release.set()
        assert (await first)['status'] == 'done'
        r.draft.assert_awaited_once()
    finally:
        release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_uuid_racing_across_books_commits_only_one_original_request(run_book, monkeypatch):
    r = run_book
    other_project, other_volume, other_chapter = uuid4(), uuid4(), uuid4()
    async with r.sessions() as db:
        db.add(Project(id=other_project, title='竞争的另一部作品'))
        await db.flush()
        db.add(Volume(id=other_volume, project_id=other_project, title='另一卷', volume_idx=1))
        await db.flush()
        db.add(Chapter(id=other_chapter, volume_id=other_volume, title='另一章', chapter_idx=1,
                       content_text=r.original, global_idx=1))
        await db.commit()
    original_prepare = submissions.prepare_run_submission
    reached = 0
    both = asyncio.Event()
    async def synchronized(db, intent):
        nonlocal reached
        receipt = await original_prepare(db, intent)
        reached += 1
        if reached == 2: both.set()
        await asyncio.wait_for(both.wait(), 5)
        return receipt
    monkeypatch.setattr(submissions, 'prepare_run_submission', synchronized)
    outcomes = await asyncio.wait_for(asyncio.gather(
        start(r), start(r, project_id=other_project, chapter_id=other_chapter), return_exceptions=True,
    ), 10)
    successful = [value for value in outcomes if isinstance(value, dict)]
    assert len(successful) == 1
    assert any(isinstance(value, GenerationExecutionUnavailable) for value in outcomes)
    r.draft.assert_awaited_once()
    async with r.sessions() as reader:
        receipt = await reader.get(GenerationRunSubmission, r.request)
        run = await reader.get(GenerationRun, r.request)
        assert receipt.project_id == run.project_id == successful[0]['project_id']
        assert receipt.chapter_id == run.chapter_id
        assert await reader.scalar(select(func.count()).select_from(GenerationRun)) == 1
        assert await reader.scalar(select(func.count()).select_from(GenerationRunSubmission)) == 1


@pytest.mark.asyncio
async def test_lost_initial_pg_commit_ack_requires_read_or_explicit_resume(run_book, monkeypatch):
    r = run_book
    original_commit = GenerationExecutionSession.commit
    async def lose_ack(db):
        initial = any(isinstance(row, GenerationRunSubmission) for row in db.new)
        await original_commit(db)
        if initial:
            db.guard.revoke()
            raise GenerationExecutionUnavailable()
    monkeypatch.setattr(GenerationExecutionSession, 'commit', lose_ack)
    with pytest.raises(GenerationExecutionUnavailable) as failure: await start(r)
    assert failure.value.run_id == str(r.request)
    r.draft.assert_not_awaited()
    observed = await start(r)
    assert observed['id'] == r.request and observed['phase'] == 'planning'
    r.draft.assert_not_awaited()
    monkeypatch.setattr(GenerationExecutionSession, 'commit', original_commit)
    async with r.sessions() as caller:
        result = await runner.execute_run(run_id=str(r.request), db=caller, resume=True)
    assert result['status'] == 'done'
    r.draft.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['chapter', 'volume'])
async def test_pg_cascade_keeps_fixed_request_until_permanent_book_delete(run_book, target):
    r = run_book
    await start(r)
    async with r.sessions() as db:
        receipt = await db.get(GenerationRunSubmission, r.request)
        before = deepcopy(receipt.source_json)
        if target == 'chapter':
            await db.execute(delete(Chapter).where(Chapter.id == r.chapter))
        else:
            volume_id = (await db.get(Chapter, r.chapter)).volume_id
            await db.execute(delete(Volume).where(Volume.id == volume_id))
        await db.commit()
    with pytest.raises(submissions.RunSubmissionError) as failure: await start(r)
    assert failure.value.status == 410
    async with r.sessions() as db:
        assert await db.get(GenerationRun, r.request) is None
        assert (await db.get(GenerationRunSubmission, r.request)).source_json == before
        await db.execute(delete(Project).where(Project.id == r.project))
        await db.commit()
    async with r.sessions() as db:
        assert await db.get(GenerationRunSubmission, r.request) is None
    r.draft.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_capture_waits_for_real_chapter_row_then_compares_committed_body(run_book):
    r = run_book
    waiting = asyncio.Event()
    def before_query(_connection, _cursor, statement, _params, _context, _many):
        if 'FROM chapters' in statement and 'FOR SHARE' in statement:
            waiting.set()
    event.listen(r.engine.sync_engine, 'before_cursor_execute', before_query)
    pending = None
    try:
        async with r.sessions() as author:
            chapter = await author.get(Chapter, r.chapter)
            chapter.content_text = '作者正在提交的完整新稿。'
            await author.flush()
            pending = asyncio.create_task(start(r))
            await asyncio.wait_for(waiting.wait(), 5)
            assert not pending.done()
            await author.commit()
        with pytest.raises(submissions.RunSubmissionError) as conflict:
            await asyncio.wait_for(pending, 5)
        assert conflict.value.detail['code'] == 'chapter_edit_conflict'
        async with r.sessions() as reader:
            assert await reader.scalar(select(func.count()).select_from(GenerationRun)) == 0
            assert await reader.scalar(select(func.count()).select_from(GenerationRunSubmission)) == 0
        r.draft.assert_not_awaited()
    finally:
        if pending is not None:
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(r.engine.sync_engine, 'before_cursor_execute', before_query)


@pytest.mark.asyncio
async def test_pg_downgrade_rechecks_after_concurrent_acceptance_commit(run_book, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    r = run_book
    path = Path(__file__).parents[1] / 'alembic/versions/a1001954_generation_run_submissions.py'
    spec = importlib.util.spec_from_file_location('run_receipt_downgrade_race', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    reached_check = asyncio.Event()
    marker = 'run_submission_downgrade_test'
    def before_query(connection, _cursor, statement, _params, _context, _many):
        if connection.info.get(marker) and statement.startswith('LOCK TABLE generation_run_submissions'):
            reached_check.set()
    def after_query(connection, _cursor, statement, _params, _context, _many):
        # Without the new lock, allow the old empty-table SELECT to finish
        # before the concurrent author transaction commits. DROP then loses
        # the receipt, so this test must refuse that former implementation.
        if (connection.info.get(marker) and statement.startswith('SELECT')
                and 'generation_run_submissions' in statement):
            reached_check.set()
    event.listen(r.engine.sync_engine, 'before_cursor_execute', before_query)
    event.listen(r.engine.sync_engine, 'after_cursor_execute', after_query)
    pending = None
    async def downgrade():
        async with r.engine.begin() as connection:
            connection.sync_connection.info[marker] = True
            try:
                def apply(sync):
                    monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(sync)))
                    migration.downgrade()
                await connection.run_sync(apply)
            finally:
                connection.sync_connection.info.pop(marker, None)
    try:
        async with r.sessions() as author:
            chapter = await author.get(Chapter, r.chapter)
            volume = await author.get(Volume, chapter.volume_id)
            intent = submissions.run_submission_intent(request_id=r.request, project_id=r.project,
                chapter_id=r.chapter, max_rewrite_count=0, expected_content_text=r.original)
            author.add(submissions.make_run_submission(intent, chapter, volume))
            author.add(GenerationRun(id=r.request, project_id=r.project, chapter_id=r.chapter,
                                     max_rewrite_count=0, phase='planning', status='running'))
            await author.flush()
            pending = asyncio.create_task(downgrade())
            await asyncio.wait_for(reached_check.wait(), 5)
            await author.commit()
        with pytest.raises(RuntimeError, match='固定请求回执'):
            await asyncio.wait_for(pending, 5)
        async with r.sessions() as reader:
            assert await reader.get(GenerationRunSubmission, r.request) is not None
            assert await reader.get(GenerationRun, r.request) is not None
    finally:
        if pending is not None:
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(r.engine.sync_engine, 'before_cursor_execute', before_query)
        event.remove(r.engine.sync_engine, 'after_cursor_execute', after_query)
