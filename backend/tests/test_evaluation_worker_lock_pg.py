"""Real PostgreSQL lock and bound-session integration gate."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, EvaluateTask
from app.services.chapter_evaluator import EvaluationResult
from app.services.evaluation_neighbors import EvaluationNeighbors
from app.tasks import evaluation_tasks


@pytest.mark.asyncio
async def test_duplicate_delivery_skips_model_and_model_wait_has_no_transaction(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c))
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '完整正文。'
        task = EvaluateTask(chapter_id=r.chapter, status='pending')
        db.add(task)
        await db.commit()
        tid = str(task.id)
    from app.db import session
    monkeypatch.setattr(session, 'engine', r.engine)
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value={}))
    monkeypatch.setattr('app.services.character_cognition.serialize_for_prompt', lambda rows, **kwargs: '')
    monkeypatch.setattr('app.services.style_stat.load_style_stats_text', AsyncMock(return_value=''))
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors', AsyncMock(return_value=EvaluationNeighbors()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    leases = []
    original = evaluation_tasks._evaluate_task_owned
    async def owned(tid, cid, caller, lease):
        leases.append(lease)
        return await original(tid, cid, caller, lease)
    monkeypatch.setattr(evaluation_tasks, '_evaluate_task_owned', owned)
    entered, release = asyncio.Event(), asyncio.Event()
    async def evaluate(**kwargs):
        assert not leases[0].connection.in_transaction()
        entered.set()
        await release.wait()
        return EvaluationResult(overall=8)
    call = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr('app.services.chapter_evaluator.ChapterEvaluator.evaluate', call)
    first = asyncio.create_task(evaluation_tasks._run_evaluate_task_async(tid, str(r.chapter), 'test'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await evaluation_tasks._run_evaluate_task_async(tid, str(r.chapter), 'test')
        assert duplicate['reason'] == 'already_running'
    finally:
        release.set()
        await first
    call.assert_awaited_once()
    async with r.sessions() as db:
        stored = await db.get(EvaluateTask, tid)
        assert stored.status == 'completed' and stored.result_json['overall'] == 8
