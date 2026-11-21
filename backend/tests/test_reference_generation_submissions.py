"""The legacy reference-outline adapter shares durable async submissions."""
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from tests.test_generation_submissions import book, queued, tasks


@pytest.fixture
def reference(queued):
    r = queued
    r.reference_request = dict(request_id=str(uuid4()), project_id=str(r.pid),
        reference_book_id=str(uuid4()), intent='写一部人物动机清楚的长篇。',
        style_hint='自然叙述', target_volumes=12, target_chapters_per_volume=150)
    def publish(*, args, task_id, retry):
        assert args == [task_id] and retry is False
        with Session(r.engine) as reader:
            task = reader.get(GenerationTask, UUID(task_id))
            assert task.task_type == 'outline_from_reference'
            assert task.params_json['submission']['fingerprint']
            assert task.params_json['reference_book_id'] == r.reference_request['reference_book_id']
        return NS(id=task_id)
    r.publish.side_effect = publish
    return r


async def submit(r, **changes):
    async with r.client() as client:
        return await client.post('/api/outlines/from-reference/start', json={**r.reference_request, **changes})


@pytest.mark.asyncio
async def test_reference_submission_commits_before_dispatch_and_reuses_the_same_request(reference):
    response = await submit(reference)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['task_id'] == reference.reference_request['request_id']
    assert receipt['project_id'] == str(reference.pid) and receipt['task_type'] == 'outline_from_reference'
    assert (await submit(reference)).json() == receipt
    assert len(tasks(reference)) == 1 and reference.publish.call_count == 2
    original = tasks(reference)[0]
    original.status = 'completed'
    original.params_json = {**original.params_json, 'reference_book': {'title': '参考书'}}
    original.result_text = '已经生成的候选大纲。'
    reference.session.commit()
    assert (await submit(reference)).json()['status'] == 'completed'
    assert reference.publish.call_count == 2
    async with reference.client() as client:
        response = await client.get(f'/api/outlines/from-reference/tasks/{original.id}')
    assert response.status_code == 200
    assert response.json()['result_text'] == '已经生成的候选大纲。'
    assert response.json()['reference_book'] == {'title': '参考书'}


@pytest.mark.asyncio
async def test_reference_and_general_async_adapters_recognize_one_request(reference):
    assert (await submit(reference)).status_code == 200
    params = {key: value for key, value in reference.reference_request.items() if key not in {'request_id', 'project_id'}}
    async with reference.client() as client:
        response = await client.post('/api/generate/async', json={
            'project_id': str(reference.pid), 'request_id': reference.reference_request['request_id'],
            'task_type': 'outline_from_reference', 'params': params,
        })
    assert response.status_code == 200, response.text
    assert len(tasks(reference)) == 1


@pytest.mark.asyncio
async def test_reference_submission_requires_a_request_id(reference):
    del reference.reference_request['request_id']
    response = await submit(reference)
    assert response.status_code == 428 and not tasks(reference)
    reference.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [dict(reference_book_id=str(uuid4())), dict(intent='改成另一个故事。'),
    dict(target_chapters_per_volume=120), dict(project_id=str(uuid4()))])
async def test_reference_request_cannot_be_rebound(reference, change):
    assert (await submit(reference)).status_code == 200
    response = await submit(reference, **change)
    assert response.status_code == 409 and len(tasks(reference)) == 1
    reference.publish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before', 'after'])
async def test_reference_commit_uncertainty_returns_original_id_without_publishing(reference, mode):
    reference.fail_commit = mode == 'before'
    reference.lose_ack = mode == 'after'
    response = await submit(reference)
    assert response.status_code == 503
    assert response.json()['detail']['task_id'] == reference.reference_request['request_id']
    assert 'private' not in response.text
    reference.publish.assert_not_called()
    assert len(tasks(reference)) == (0 if mode == 'before' else 1)
    assert (await submit(reference)).status_code == 200 and len(tasks(reference)) == 1


@pytest.mark.asyncio
async def test_reference_cancel_marker_survives_a_lost_dispatch_ack(reference):
    def publish(**kwargs):
        with Session(reference.engine) as writer:
            task = writer.get(GenerationTask, UUID(kwargs['task_id']))
            task.status = 'running'
            task.params_json = {**task.params_json, 'cancelled_at': '2026-10-09'}
            writer.commit()
        raise TimeoutError('private broker error')
    reference.publish.side_effect = publish
    assert (await submit(reference)).status_code == 503
    assert (await submit(reference)).json()['status'] == 'cancelled'
    assert reference.publish.call_count == 1
    async with reference.client() as client:
        response = await client.get(f'/api/outlines/from-reference/tasks/{reference.reference_request["request_id"]}')
    assert response.status_code == 200 and response.json()['status'] == 'cancelled'
    assert response.json()['project_id'] == str(reference.pid)


@pytest.mark.asyncio
async def test_reference_poll_cannot_present_a_chapter_task_as_an_outline(reference):
    task = GenerationTask(project_id=reference.pid, task_type='chapter', status='pending')
    reference.session.add(task); reference.session.commit()
    async with reference.client() as client:
        response = await client.get(f'/api/outlines/from-reference/tasks/{task.id}')
    assert response.status_code == 404
