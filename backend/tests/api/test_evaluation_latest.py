import uuid
from datetime import datetime, timezone, timedelta

import pytest
pytestmark = pytest.mark.external_services
from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.project import Chapter, ChapterEvaluation, EvaluateTask
from tests.support.chapter_fixture import create_chapter_target


@pytest.mark.asyncio
async def test_latest_combines_saved_and_async_reviews_without_generating(auth_client, monkeypatch):
    from unittest.mock import Mock
    dispatch = Mock(side_effect=AssertionError('read endpoint must not enqueue'))
    monkeypatch.setattr('app.tasks.evaluation_tasks.dispatch_evaluate_task', dispatch)
    project = (await auth_client.post('/api/projects', json={'title': '评审历史测试', 'genre': '测试'})).json()['id']
    try:
        volume = await create_chapter_target(project)
        async with async_session_factory() as db:
            chapter = await db.scalar(select(Chapter).where(Chapter.volume_id == uuid.UUID(volume)))
            cid = str(chapter.id)
        url = f'/api/evaluate/chapters/{cid}/latest'
        assert (await auth_client.get(url)).json() is None
        now = datetime.now(timezone.utc)
        async with async_session_factory() as db:
            saved = ChapterEvaluation(chapter_id=cid, overall=9, issues_json=[], created_at=now)
            task = EvaluateTask(chapter_id=cid, status='pending', created_at=now + timedelta(seconds=1))
            db.add_all([saved, task]);await db.commit()
            sid, tid = saved.id, task.id
        response = await auth_client.get(url)
        assert response.status_code == 200
        assert response.json()['task_id'] == str(tid)
        assert response.json()['status'] == 'pending'
        async with async_session_factory() as db:
            task = await db.get(EvaluateTask, tid)
            task.status = 'completed';task.result_json = {'overall': 0, 'parse_failed': True, 'failure_reason': 'call_failed'}
            await db.commit()
        assert (await auth_client.get(url)).json()['result']['failure_reason'] == 'call_failed'
        async with async_session_factory() as db:
            saved = await db.get(ChapterEvaluation, sid)
            saved.created_at = now + timedelta(seconds=2)
            saved.issues_json = [{'dimension': 'system', 'failure_reason': 'timeout'}]
            await db.commit()
        latest = (await auth_client.get(url)).json()
        assert latest['caller'] == 'saved_chapter_evaluation'
        assert latest['result']['parse_failed'] is True
        assert latest['result']['failure_reason'] == 'timeout'
        assert (await auth_client.get(f'/api/evaluate/chapters/{uuid.uuid4()}/latest')).status_code == 404
        assert (await auth_client.get('/api/evaluate/chapters/not-a-uuid/latest')).status_code == 422
        dispatch.assert_not_called()
    finally:
        await auth_client.delete(f'/api/projects/{project}')
