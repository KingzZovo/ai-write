from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api.evaluate import EvaluateStartRequest, start_evaluate_task


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'running'])
async def test_existing_active_task_is_reused_without_dispatch(monkeypatch, status):
    cid, tid = uuid4(), uuid4()
    chapter = NS(id=cid, content_text='已保存正文。')
    active = NS(id=tid, chapter_id=cid, status=status, round_idx=2)
    db = NS(scalar=AsyncMock(side_effect=[chapter, active]), commit=AsyncMock(), add=Mock())
    dispatch = Mock()
    monkeypatch.setattr('app.tasks.evaluation_tasks.dispatch_evaluate_task', dispatch)
    result = await start_evaluate_task(EvaluateStartRequest(chapter_id=str(cid)), db)
    assert result.task_id == str(tid) and result.status == status and result.round_idx == 2
    assert 'FOR UPDATE' in str(db.scalar.call_args_list[0].args[0])
    query = db.scalar.call_args_list[1].args[0]
    params = query.compile().params
    assert cid in params.values() and any(v == ['pending', 'running'] for v in params.values())
    db.commit.assert_awaited_once()
    db.add.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_new_task_is_committed_before_dispatch(monkeypatch):
    cid, tid = uuid4(), uuid4()
    events, rows = [], []
    async def refresh(row): row.id = tid
    async def commit(): events.append('commit')
    db = NS(scalar=AsyncMock(side_effect=[NS(id=cid, content_text='正文。'), None]),
            add=rows.append, flush=AsyncMock(), refresh=AsyncMock(side_effect=refresh),
            commit=AsyncMock(side_effect=commit))
    def dispatch(**kwargs):
        assert events == ['commit']
        events.append('dispatch')
        return True
    monkeypatch.setattr('app.tasks.evaluation_tasks.dispatch_evaluate_task', dispatch)
    result = await start_evaluate_task(EvaluateStartRequest(chapter_id=str(cid)), db)
    assert len(rows) == 1 and result.task_id == str(tid) and result.status == 'pending'
    assert events == ['commit', 'dispatch']
