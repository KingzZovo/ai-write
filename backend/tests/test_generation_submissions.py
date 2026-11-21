"""Fixed submission receipts and worker claims using SQLite and real ASGI.

Queue/async-thread handoff and the PG advisory lease are explicit substitutes.
The actual PG contention cases live in test_generation_submissions_pg.py.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.api import generate, generation_runs
from app.db.session import get_db
from app.models.generation_task import GenerationTask
from app.models.project import Chapter, Outline, Project, Volume
from app.services import generation_submissions as submissions
from app.services.production.fencing import StaleChapterWrite
from tests.test_generation_execution import LocalSession
from tests.test_version_application import BEFORE, book


@pytest.fixture
def queued(book, monkeypatch):
    r = book
    for model in (Outline, GenerationTask): model.__table__.create(r.engine)
    chapter = r.session.get(Chapter, r.cid)
    chapter.outline_json = {'summary': '把两千元交给弟弟。'}
    r.session.add_all([
        Outline(project_id=r.pid, level='book', content_json={'summary': '现实题材的家庭故事。'}),
        Outline(project_id=r.pid, level='volume', content_json={
            'volume_idx': 1, 'volume_id': str(chapter.volume_id), 'summary': '回家，交钱，带弟弟看腿。'}),
    ])
    r.session.commit()
    r.request = dict(request_id=str(uuid4()), project_id=str(r.pid), task_type='chapter',
                     chapter_id=str(r.cid), expected_content_text=BEFORE)
    def publish(*, args, task_id, retry):
        assert args == [task_id] and retry is False
        with Session(r.engine) as reader:
            task = reader.get(GenerationTask, UUID(task_id))
            assert task is not None and task.params_json['celery_task_id'] == task_id
            assert task.params_json['submission']['chapter_source']
        return NS(id=task_id)
    r.publish = Mock(side_effect=publish)
    monkeypatch.setattr('app.tasks.knowledge_tasks.run_async_generation.apply_async', r.publish)
    monkeypatch.setattr(generate.asyncio, 'to_thread', AsyncMock(side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs)))
    async def database():
        try:
            yield r.db
            await r.db.commit()
        except BaseException:
            await r.db.rollback()
            raise
    app = FastAPI()
    app.include_router(generate.router)
    app.include_router(generation_runs.router)
    app.dependency_overrides[get_db] = database
    r.client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test')
    return r


async def submit(r, **changes):
    async with r.client() as client:
        return await client.post('/api/generate/async', json={**r.request, **changes})


def tasks(r):
    r.session.expire_all()
    return list(r.session.scalars(sa.select(GenerationTask)))


@pytest.mark.asyncio
async def test_receipt_is_fixed_and_visible_before_publication(queued):
    response = await submit(queued)
    assert response.status_code == 200, response.text
    assert response.json() == dict(task_id=queued.request['request_id'], project_id=str(queued.pid),
        task_type='chapter', chapter_id=str(queued.cid), volume_idx=None, status='pending')
    task = tasks(queued)[0]
    assert task.params_json['submission']['chapter_source'] == submissions.chapter_submission_source(queued.session.get(Chapter, queued.cid))
    assert 'expected_content_text' not in task.params_json
    assert BEFORE not in json.dumps(task.params_json, ensure_ascii=False)
    queued.publish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['request_id', 'expected_content_text'])
async def test_missing_precondition_does_not_create_a_task(queued, missing):
    del queued.request[missing]
    response = await submit(queued)
    assert response.status_code == 428
    assert not tasks(queued)
    queued.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['content', 'last_character'])
async def test_stale_author_body_cannot_become_the_new_baseline(queued, kind):
    chapter = queued.session.get(Chapter, queued.cid)
    chapter.content_text = ('作者刚补写了收钱的动作。' + BEFORE) if kind == 'content' else BEFORE[:-1] + '！'
    queued.session.commit()
    response = await submit(queued, params={'force': True})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'chapter_edit_conflict'
    assert not tasks(queued)
    queued.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(user_input='另一个要求'), dict(params={'target_words': 2400}),
    dict(expected_content_text='另一份原稿'), dict(project_id=str(uuid4()))])
async def test_request_id_cannot_be_rebound_to_different_intent(queued, change):
    assert (await submit(queued)).status_code == 200
    response = await submit(queued, **change)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'generation_request_conflict'
    assert len(tasks(queued)) == 1
    queued.publish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['running', 'evaluating', 'completed', 'needs_review', 'needs_repair', 'failed', 'cancelled'])
async def test_repeating_an_accepted_request_reads_original_without_restarting_it(queued, status):
    assert (await submit(queued)).status_code == 200
    original = tasks(queued)[0]
    original.status = status
    queued.session.get(Project, queued.pid).settings_json = {'style_profile_id': str(uuid4())}
    queued.session.get(Chapter, queued.cid).content_text = '提交以后作者改过的原稿。'
    queued.session.commit()
    response = await submit(queued)
    assert response.status_code == 200 and response.json()['status'] == status
    assert response.json()['task_id'] == str(original.id) and len(tasks(queued)) == 1
    queued.publish.assert_called_once()


@pytest.mark.asyncio
async def test_pending_retry_keeps_original_source_and_task(queued):
    assert (await submit(queued)).status_code == 200
    old = deepcopy(tasks(queued)[0].params_json)
    queued.session.get(Chapter, queued.cid).content_text = '排队期间作者的新稿。'
    queued.session.commit()
    assert (await submit(queued)).status_code == 200
    assert len(tasks(queued)) == 1 and tasks(queued)[0].params_json == old
    assert queued.publish.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_commit_uncertainty_preserves_fixed_recovery_id_without_publishing(queued, mode):
    queued.fail_commit = mode == 'before'
    queued.lose_ack = mode == 'after'
    response = await submit(queued)
    assert response.status_code == 503 and response.json()['detail']['task_id'] == queued.request['request_id']
    assert 'private' not in response.text
    assert len(tasks(queued)) == (0 if mode == 'before' else 1)
    queued.publish.assert_not_called()
    assert (await submit(queued)).status_code == 200
    assert len(tasks(queued)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_status', ['pending', 'running', 'completed', 'cancelled'])
async def test_lost_broker_ack_does_not_overwrite_a_worker_or_cancel(queued, worker_status):
    def publish(**kwargs):
        with Session(queued.engine) as writer:
            task = writer.get(GenerationTask, UUID(kwargs['task_id']))
            task.status = worker_status
            task.params_json = {**task.params_json, 'worker_receipt': 'keep', **({'cancelled_at': '2026-10-09'} if worker_status == 'cancelled' else {})}
            writer.commit()
        raise TimeoutError('private broker error')
    queued.publish.side_effect = publish
    response = await submit(queued)
    assert response.status_code == 503
    task = tasks(queued)[0]
    assert task.status == worker_status and task.params_json['worker_receipt'] == 'keep'
    assert 'private' not in response.text


@pytest.mark.asyncio
async def test_successful_late_publisher_cannot_erase_cancel_marker(queued):
    def publish(**kwargs):
        with Session(queued.engine) as writer:
            task = writer.get(GenerationTask, UUID(kwargs['task_id']))
            task.status = 'cancelled'
            task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
            writer.commit()
        return NS(id=kwargs['task_id'])
    queued.publish.side_effect = publish
    assert (await submit(queued)).status_code == 200
    assert tasks(queued)[0].params_json['cancelled_at'] == '2026-10-09'
    assert (await submit(queued)).json()['status'] == 'cancelled'
    queued.publish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [{'submission': {}}, {'celery_task_id': 'foreign'}, {'cancelled_at': 'now'},
    {'pipeline_id': str(uuid4())},
    {'chapter_id': str(uuid4())}, {'volume_idx': True}])
async def test_extra_params_cannot_replace_identity_source_or_task_control(queued, params):
    response = await submit(queued, params=params)
    assert response.status_code == 422 and not tasks(queued)
    queued.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['trash', 'missing_project', 'foreign_chapter', 'missing_chapter'])
async def test_new_submission_checks_live_project_and_chapter_scope(queued, kind):
    changes = {}
    if kind == 'trash':
        queued.session.get(Project, queued.pid).deleted_at = datetime.now(timezone.utc)
        queued.session.commit()
    elif kind == 'missing_project': changes['project_id'] = str(uuid4())
    elif kind == 'missing_chapter': changes['chapter_id'] = str(uuid4())
    else:
        other = Project(id=uuid4(), title='另一部作品')
        queued.session.add(other); queued.session.commit()
        changes['project_id'] = str(other.id)
    assert (await submit(queued, **changes)).status_code == 404
    assert not tasks(queued)


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_chapter'])
@pytest.mark.parametrize('target', [dict(volume_idx=2), dict(chapter_idx=2)])
async def test_explicit_chapter_position_must_match_the_current_target(queued, task_type, target):
    if task_type != 'chapter': queued.publish.side_effect = None
    response = await submit(queued, task_type=task_type, **target)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'generation_request_invalid'
    assert not tasks(queued)
    queued.publish.assert_not_called()


@pytest.mark.asyncio
async def test_matching_explicit_chapter_position_is_accepted(queued):
    response = await submit(queued, volume_idx=1, chapter_idx=1)
    assert response.status_code == 200 and response.json()['volume_idx'] == 1
    assert len(tasks(queued)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('layer', ['book', 'volume', 'chapter'])
async def test_outline_chain_still_blocks_before_publish(queued, layer):
    if layer == 'chapter': queued.session.get(Chapter, queued.cid).outline_json = {}
    else: queued.session.execute(sa.delete(Outline).where(Outline.level == layer))
    queued.session.commit()
    response = await submit(queued)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'outline_chain_incomplete'
    assert not tasks(queued)
    queued.publish.assert_not_called()


@pytest.mark.asyncio
async def test_source_receipt_is_bounded_and_covers_the_entire_manuscript(queued):
    chapter = queued.session.get(Chapter, queued.cid)
    chapter.content_text = '正文资料。' * 1_000_001
    first = submissions.chapter_submission_source(chapter)
    chapter.content_text += '末尾新句。'
    second = submissions.chapter_submission_source(chapter)
    assert first != second and len(json.dumps(second)) < 250


@pytest.fixture
def worker(queued, monkeypatch):
    from app.db import session as db_module
    from app.services.production import writer
    from app.tasks import generation_tasks
    from tests.support.independent_task_runtime import install_runtime
    queued.execution = install_runtime(monkeypatch, queued.engine)
    class WorkerSession(LocalSession):
        async def scalars(self, query): return self.session.scalars(query)
        def expire_all(self): self.session.expire_all()
    @asynccontextmanager
    async def sessions():
        with Session(queued.engine, expire_on_commit=False) as session:
            yield WorkerSession(session)
    @asynccontextmanager
    async def lease(_engine, _project):
        yield NS(check=AsyncMock())
    monkeypatch.setattr(db_module, 'async_session_factory', sessions)
    monkeypatch.setattr(db_module._state, 'engine', queued.engine)
    monkeypatch.setattr(writer, 'project_worker_lock', lease)
    async def implementation(task_id, *, session_factory, read_session_factory=None):
        async with session_factory() as db:
            task = await db.get(GenerationTask, task_id)
            assert task.status == 'running'
            task.status = 'completed'
            await db.commit()
    queued.implementation = AsyncMock(side_effect=implementation)
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', queued.implementation)
    queued.run = generation_tasks._run_guarded_generation
    queued.sessions = sessions
    return queued


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['content_text', 'title', 'outline_json', 'chapter_idx', 'global_idx', 'summary', 'status', 'target_word_count'])
async def test_worker_rejects_source_changes_during_queue_without_model_calls(worker, field):
    assert (await submit(worker)).status_code == 200
    chapter = worker.session.get(Chapter, worker.cid)
    value = {'outline_json': {'summary': '变了'}, 'chapter_idx': 3, 'global_idx': 3, 'target_word_count': 2400}.get(field, '作者后来的修改')
    setattr(chapter, field, value)
    worker.session.commit()
    with pytest.raises(StaleChapterWrite, match='提交后'):
        await worker.run(worker.request['request_id'])
    worker.implementation.assert_not_awaited()
    assert tasks(worker)[0].status == 'failed'
    assert getattr(worker.session.get(Chapter, worker.cid), field) == value


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [None, {}, {'version': True}, {'version': 1, 'chapter_id': 'wrong', 'material_hash': 'a' * 64}])
async def test_old_or_invalid_chapter_receipt_cannot_be_guessed_at_execution(worker, source):
    assert (await submit(worker)).status_code == 200
    task = tasks(worker)[0]
    task.params_json = {**task.params_json, 'submission': {'chapter_source': source}}
    worker.session.commit()
    with pytest.raises(StaleChapterWrite, match='缺少'):
        await worker.run(str(task.id))
    worker.implementation.assert_not_awaited()
    assert worker.session.get(Chapter, worker.cid).content_text == BEFORE


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_book'])
async def test_repeated_deliveries_claim_only_one_execution(worker, task_type):
    if task_type == 'outline_book': worker.publish.side_effect = None
    assert (await submit(worker, task_type=task_type)).status_code == 200
    await asyncio.gather(worker.run(worker.request['request_id']), worker.run(worker.request['request_id']))
    assert worker.implementation.await_count == 1
    assert tasks(worker)[0].status == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('status, marker', [('running', None), ('pending', '2026-10-09'), ('needs_repair', None), ('cancelled', None)])
async def test_running_or_cancelled_tasks_are_not_reclaimed(worker, status, marker):
    assert (await submit(worker)).status_code == 200
    task = tasks(worker)[0]
    task.status = status
    task.params_json = {**task.params_json, **({'cancelled_at': marker} if marker else {})}
    worker.session.commit()
    await worker.run(str(task.id))
    worker.implementation.assert_not_awaited()
    async with worker.sessions() as db:
        assert not await submissions.claim_generation_submission(db, task.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_cancellation_receipt_requires_commit_and_does_not_revoke_early(queued, monkeypatch, mode):
    assert (await submit(queued)).status_code == 200
    revoke = Mock()
    monkeypatch.setattr('app.tasks.celery_app.control.revoke', revoke)
    queued.fail_commit = mode == 'before'
    queued.lose_ack = mode == 'after'
    async with queued.client() as client:
        response = await client.post(f'/api/generate/async/{queued.request["request_id"]}/cancel')
    assert response.status_code == 503 and response.json()['detail']['code'] == 'generation_cancel_unconfirmed'
    assert tasks(queued)[0].status == ('pending' if mode == 'before' else 'cancelled')
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_commits_current_params_before_best_effort_revoke(queued, monkeypatch):
    assert (await submit(queued)).status_code == 200
    task = tasks(queued)[0]
    with Session(queued.engine) as db:
        row = db.get(GenerationTask, task.id)
        row.params_json = {**row.params_json, 'worker_receipt': 'new'}
        db.commit()
    def revoke(task_id, *, terminate):
        assert not terminate
        with Session(queued.engine) as db:
            row = db.get(GenerationTask, UUID(task_id))
            assert row.status == 'cancelled' and row.params_json['cancelled_at']
            assert row.params_json['worker_receipt'] == 'new'
    callback = Mock(side_effect=revoke)
    monkeypatch.setattr('app.tasks.celery_app.control.revoke', callback)
    async with queued.client() as client:
        response = await client.post(f'/api/generate/async/{task.id}/cancel')
    assert response.status_code == 200
    callback.assert_called_once_with(str(task.id), terminate=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['needs_review', 'needs_repair'])
async def test_review_and_repair_results_are_terminal_for_cancellation(queued, status):
    assert (await submit(queued)).status_code == 200
    task = tasks(queued)[0]
    task.status = status
    queued.session.commit()
    async with queued.client() as client:
        response = await client.post(f'/api/generate/async/{task.id}/cancel')
    assert response.status_code == 409 and tasks(queued)[0].status == status


@pytest.mark.asyncio
async def test_late_outline_task_result_cannot_erase_accepted_cancellation(worker):
    worker.publish.side_effect = None
    assert (await submit(worker, task_type='outline_book')).status_code == 200
    async def implementation(task_id, *, session_factory, read_session_factory=None):
        async with session_factory() as db:
            row = await db.get(GenerationTask, task_id)
            def author_cancel():
                with Session(worker.engine) as other:
                    task = other.get(GenerationTask, UUID(task_id))
                    task.status = 'cancelled'
                    task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
                    other.commit()
            from contextvars import Context
            Context().run(author_cancel)
            row.params_json = {**row.params_json, 'generated': True}
            row.result_text = '取消以后到达的旧结果。'
            row.status = 'completed'
            await db.commit()
    worker.implementation.side_effect = implementation
    from app.services.generation_execution import GenerationExecutionUnavailable
    # The pinned write session revokes itself after an unconfirmed/failed
    # commit, including a cancellation rejected by the before-flush fence.
    with pytest.raises(GenerationExecutionUnavailable):
        await worker.run(worker.request['request_id'])
    row = tasks(worker)[0]
    assert row.status == 'cancelled' and row.params_json['cancelled_at'] == '2026-10-09'
    assert not row.result_text
