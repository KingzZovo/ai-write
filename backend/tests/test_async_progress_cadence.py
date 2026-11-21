from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.tasks import generation_tasks as tasks


@pytest.mark.asyncio
async def test_small_chunks_do_not_multiply_slow_commits(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tasks, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    async def commit(): clock[0] += .4
    db = SimpleNamespace(commit=AsyncMock(side_effect=commit))
    task = SimpleNamespace(progress_text='', char_count=0)
    writer = tasks._ProgressWriter()
    pieces = []
    text = Mock(side_effect=lambda: ''.join(pieces))
    for _ in range(1000):
        pieces.append('字');clock[0] += .001
        await writer.write(db, task, text)
    assert db.commit.await_count == 1
    assert text.call_count == 1
    assert len(pieces) == 1000
    clock[0] += 2
    await writer.write(db, task, text)
    assert task.progress_text == '字' * 1000 and task.char_count == 1000
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_each_task_has_its_own_cadence_and_failures_are_not_marked_written(monkeypatch):
    monkeypatch.setattr(tasks, 'time', SimpleNamespace(monotonic=lambda: 1.0))
    task = SimpleNamespace()
    db = SimpleNamespace(commit=AsyncMock(side_effect=[RuntimeError('DB unavailable'), None, None]))
    writer = tasks._ProgressWriter()
    with pytest.raises(RuntimeError): await writer.write(db, task, lambda: 'partial')
    await writer.write(db, task, lambda: 'retry')
    await tasks._ProgressWriter().write(db, SimpleNamespace(), lambda: 'other task')
    assert db.commit.await_count == 3
