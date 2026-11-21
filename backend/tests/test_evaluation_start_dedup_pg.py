import asyncio
from unittest.mock import Mock

import pytest
from sqlalchemy import select, func
from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, EvaluateTask
from app.api.evaluate import EvaluateStartRequest, start_evaluate_task


@pytest.mark.asyncio
async def test_concurrent_starts_create_and_dispatch_only_one_task(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: EvaluateTask.__table__.create(c))
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.content_text = '完整已保存正文。'
        await db.commit()
    dispatch = Mock(return_value=True)
    monkeypatch.setattr('app.tasks.evaluation_tasks.dispatch_evaluate_task', dispatch)
    async def start():
        async with r.sessions() as db:
            return await start_evaluate_task(EvaluateStartRequest(chapter_id=str(r.chapter)), db)
    a, b = await asyncio.gather(start(), start())
    assert a.task_id == b.task_id
    dispatch.assert_called_once()
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(EvaluateTask)) == 1
        row = await db.get(EvaluateTask, a.task_id)
        row.status = 'completed'
        await db.commit()
    fresh = await start()
    assert fresh.task_id != a.task_id and dispatch.call_count == 2
