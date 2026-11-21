"""Call accounting against isolated PG; collected and skipped in offline mode."""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_run import CriticReport, GenerationRun
from app.models.project import Chapter
from app.services import generation_runner as runner
from app.services.generation_execution import GenerationExecutionUnavailable
from app.services.review_contract import make_critic_report
from tests.test_generation_execution_pg import DRAFT, setup_runner
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


async def start(r, request, budget=0):
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        return await runner.start_run(project_id=str(r.project), chapter_id=str(r.chapter), db=db,
            request_id=request, expected_content_text=chapter.content_text or '', max_rewrite_count=budget)


async def resume(r, request):
    async with r.sessions() as db:
        return await runner.execute_run(run_id=str(request), db=db, resume=True)


@pytest.mark.asyncio
async def test_rewrite_reservation_visible_before_call_and_not_refunded_on_resume(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    request = uuid4()
    monkeypatch.setattr(runner, '_phase_drafting', AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'}))
    issue = {'severity': 'hard', 'category': 'resource', 'desc': '核对约定金额。', 'location': '两千元'}
    monkeypatch.setattr(runner, 'run_critic', AsyncMock(return_value=make_critic_report(
        DRAFT, issues=[issue], stages=[{'name': 'critic', 'status': 'complete'}])))
    async def rewrite(_run, db, *_args):
        assert not db.in_transaction()
        async with r.sessions() as observer:
            saved = await observer.get(GenerationRun, request)
            assert saved.rewrite_count == 1
            assert saved.checkpoint_data['drafting']['text'] == DRAFT
            assert saved.checkpoint_data['attempt_journal']['attempts'][-1]['status'] == 'started'
        raise asyncio.CancelledError()
    call = AsyncMock(side_effect=rewrite)
    monkeypatch.setattr(runner, '_phase_rewrite', call)
    with pytest.raises(asyncio.CancelledError): await start(r, request, 1)
    result = await resume(r, request)
    assert result['status'] == 'needs_review' and result['rewrite_count'] == 1
    assert result['checkpoint_data']['drafting']['text'] == DRAFT
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_interruption_is_bounded_across_physical_sessions(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    request = uuid4()
    call = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(runner, '_phase_drafting', call)
    with pytest.raises(asyncio.CancelledError): await start(r, request)
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError): await resume(r, request)
    result = await resume(r, request)
    assert result['status'] == 'needs_review' and result['stop_reason'] == 'model_call_budget_exhausted'
    entries = result['checkpoint_data']['attempt_journal']['attempts']
    assert len(entries) == len({e['id'] for e in entries}) == call.await_count == 3
    assert all(e['status'] == 'interrupted' for e in entries)


@pytest.mark.asyncio
async def test_report_insert_rolls_back_with_its_unconfirmed_checkpoint(runtime_db, monkeypatch):
    r = runtime_db
    await setup_runner(r, monkeypatch)
    request = uuid4()
    monkeypatch.setattr(runner, '_phase_drafting', AsyncMock(return_value={'text': DRAFT, 'delivery_status': 'complete'}))
    original = AsyncSession.commit
    injected = False
    async def fail_after_flush(db):
        nonlocal injected
        for row in db.dirty:
            if isinstance(row, GenerationRun) and row.id == request:
                entries = row.checkpoint_data['attempt_journal']['attempts']
                if not injected and entries and entries[-1]['kind'] == 'critic' and entries[-1]['status'] == 'returned':
                    injected = True
                    await db.flush()  # Actual PG INSERT, still in the checkpoint transaction.
                    raise RuntimeError('test interrupted report commit')
        await original(db)
    monkeypatch.setattr(AsyncSession, 'commit', fail_after_flush)
    with pytest.raises(GenerationExecutionUnavailable):
        await start(r, request)
    assert injected
    async with r.sessions() as observer:
        assert await observer.scalar(select(func.count()).select_from(CriticReport).where(CriticReport.run_id == request)) == 0
        run = await observer.get(GenerationRun, request)
        assert 'critic' not in run.checkpoint_data
        assert run.checkpoint_data['attempt_journal']['attempts'][-1]['status'] == 'started'
    result = await resume(r, request)
    assert result['status'] == 'done'
    assert result['checkpoint_data']['drafting']['text'] == DRAFT
    assert runner.run_critic.await_count == 2
