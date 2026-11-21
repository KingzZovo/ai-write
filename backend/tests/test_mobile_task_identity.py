"""Offline task-source/read contracts. SQLite is not a worker/PG smoke test."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.api import generate
from app.db.session import get_db
from app.models.generation_task import GenerationTask
from app.models.project import Project
from tests.test_generation_execution import LocalSession


@pytest.fixture
def task_db(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "task-identity.sqlite"}')
    Project.__table__.create(engine)
    GenerationTask.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        project = Project(id=uuid4(), title='任务来源测试')
        session.add(project); session.commit()
        class SQLiteTaskSession(LocalSession):
            def add(self, row):
                # asyncpg accepts textual UUIDs; SQLite's UUID binder needs
                # the Python UUID value. Keep this transport adapter explicit.
                if isinstance(row, GenerationTask) and isinstance(row.project_id, str):
                    row.project_id = UUID(row.project_id)
                super().add(row)
        db = SQLiteTaskSession(session)
        async def database():
            yield db
        app = FastAPI()
        app.include_router(generate.router)
        app.dependency_overrides[get_db] = database
        yield NS(session=session, db=db, pid=project.id,
                 client=lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test'))
    engine.dispose()


def add_task(book, **overrides):
    fields = dict(id=uuid4(), project_id=book.pid, task_type='chapter', status='running',
                  params_json={'chapter_id': str(uuid4()), 'user_input': 'private-metadata-sentinel'},
                  progress_text='正在检查章节。', result_text='候选全文。' * 300,
                  polished_text='', char_count=1500)
    fields.update(overrides)
    task = GenerationTask(**fields)
    book.session.add(task); book.session.commit()
    return task


@pytest.mark.asyncio
async def test_detail_returns_persisted_identity_and_full_candidate(task_db):
    task = add_task(task_db)
    async with task_db.client() as client:
        response = await client.get(f'/api/generate/async/{task.id}')
    assert response.status_code == 200
    result = response.json()
    assert result['task_id'] == str(task.id)
    assert result['project_id'] == str(task_db.pid)
    assert result['chapter_id'] == task.params_json['chapter_id']
    assert result['task_type'] == 'chapter'
    assert result['result_text'] == task.result_text
    assert result['progress_text'] == task.progress_text
    assert 'private-metadata-sentinel' not in response.text
    assert 'params_json' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [None, [], {}, {'chapter_id': 3}, {'chapter_id': ['c']}, {'chapter_id': ''}])
async def test_missing_or_malformed_target_is_not_guessed(task_db, params):
    task = add_task(task_db, params_json=params)
    result = await generate.get_async_generation(str(task.id), db=task_db.db)
    assert result['chapter_id'] is None and result['project_id'] == str(task_db.pid)


@pytest.mark.asyncio
async def test_list_and_detail_share_identity_and_keep_candidate_out_of_list(task_db):
    task = add_task(task_db, progress_text='进度。' * 200)
    add_task(task_db, project_id=uuid4())
    # Pass UUID to SQLite's native UUID binder; HTTP detail is exercised above.
    rows = await generate.list_project_tasks(task_db.pid, limit=20, status=None, active_only=False, db=task_db.db)
    assert len(rows) == 1
    detail = await generate.get_async_generation(str(task.id), db=task_db.db)
    for key in ('task_id', 'project_id', 'chapter_id', 'task_type', 'volume_idx'):
        assert rows[0][key] == detail[key]
    assert rows[0]['progress_text'] == task.progress_text[-200:]
    assert 'result_text' not in rows[0] and 'params_json' not in rows[0]


@pytest.mark.asyncio
async def test_active_filter_finds_older_work_beyond_recent_history_and_cancel_markers(task_db):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    active = add_task(task_db, created_at=start, status='evaluating')
    for index in range(25):
        add_task(task_db, created_at=start + timedelta(minutes=index + 1), status='completed')
    for index in range(300):
        add_task(task_db, created_at=start + timedelta(hours=index + 1), status='running',
                 params_json={'chapter_id': str(uuid4()), 'cancelled_at': '2026-10-01T00:00:00Z'})
    recent = await generate.list_project_tasks(task_db.pid, limit=20, status=None, active_only=False, db=task_db.db)
    assert str(active.id) not in {row['task_id'] for row in recent}
    rows = await generate.list_project_tasks(task_db.pid, limit=1, status=None, active_only=True, db=task_db.db)
    assert [row['task_id'] for row in rows] == [str(active.id)]


@pytest.mark.asyncio
async def test_active_pagination_keeps_equal_timestamps_and_unknown_legacy_dates(task_db):
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    active = add_task(task_db, id=UUID(int=1), created_at=stamp)
    for _ in range(130):
        add_task(task_db, created_at=stamp, params_json={'cancelled_at': '2026-10-01T00:00:00Z'})
    legacy = add_task(task_db, status='future_state')
    task_db.session.execute(sa.update(GenerationTask).where(GenerationTask.id == legacy.id).values(created_at=None))
    task_db.session.commit()
    rows = await generate.list_project_tasks(task_db.pid, limit=20, status=None, active_only=True, db=task_db.db)
    assert [row['task_id'] for row in rows] == [str(active.id), str(legacy.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['completed', 'needs_review', 'needs_repair', 'cancelled', 'failed'])
async def test_all_known_terminal_states_are_absent_from_active_filter(task_db, state):
    add_task(task_db, status=state)
    rows = await generate.list_project_tasks(task_db.pid, limit=20, status=None, active_only=True, db=task_db.db)
    assert rows == []


@pytest.mark.asyncio
async def test_unknown_status_remains_visible_for_explicit_recovery(task_db):
    task = add_task(task_db, status='future_state')
    rows = await generate.list_project_tasks(task_db.pid, limit=20, status=None, active_only=True, db=task_db.db)
    assert rows[0]['task_id'] == str(task.id) and rows[0]['status'] == 'future_state'


@pytest.mark.asyncio
async def test_enqueue_receipt_binds_the_created_task_without_model_calls(task_db, monkeypatch):
    delay = Mock(return_value=NS(id='fake-broker-id'))
    monkeypatch.setattr('app.tasks.knowledge_tasks.run_async_generation.apply_async', delay)
    # This is a receipt test. The local environment cannot reliably complete
    # executor shutdown; real broker/thread handoff remains a service check.
    monkeypatch.setattr(generate.asyncio, 'to_thread', AsyncMock(side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs)))
    request = generate.AsyncGenerateRequest(request_id=str(uuid4()), project_id=str(task_db.pid), task_type='outline_book', user_input='一部现实题材小说。')
    receipt = await generate.start_async_generation(request, db=task_db.db)
    task = await task_db.db.get(GenerationTask, receipt['task_id'])
    assert receipt == dict(task_id=str(task.id), project_id=str(task_db.pid), task_type='outline_book', chapter_id=None, volume_idx=None, status='pending')
    delay.assert_called_once_with(args=[str(task.id)], task_id=str(task.id), retry=False)
    assert task.params_json['user_input'] == request.user_input


@pytest.mark.asyncio
@pytest.mark.parametrize('volume_idx, expected', [(2, 2), (True, None), ('2', None), (0, None), (-1, None), (2.5, None)])
async def test_volume_target_is_the_persisted_positive_integer_or_unknown(task_db, volume_idx, expected):
    task = add_task(task_db, task_type='outline_volume', params_json={'volume_idx': volume_idx})
    result = await generate.get_async_generation(str(task.id), db=task_db.db)
    assert result['volume_idx'] == expected
    assert result['chapter_id'] is None
