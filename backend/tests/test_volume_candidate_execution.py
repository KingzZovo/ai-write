"""Real volume worker dispatch/claim/receipt paths with a local model substitute."""
from copy import deepcopy
from contextvars import Context
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.project import Chapter, Outline, Volume
from app.services.production.fencing import StaleChapterWrite
from app.services.volume_outline_writes import VolumeOutlineError, read_volume_candidate, text_digest
from app.tasks.generation_tasks import _run_async_generation_impl as actual_implementation
from tests.test_generation_submissions import queued, worker, submit, tasks
from tests.test_version_application import book


async def submit_volume(r, **changes):
    r.publish.side_effect = None
    return await submit(r, task_type='outline_volume', chapter_id=None, volume_idx=1, expected_content_text=None, **changes)


@pytest.mark.asyncio
async def test_volume_submission_binds_saved_identity_and_repeats_original(worker):
    r = worker
    response = await submit_volume(r)
    assert response.status_code == 200, response.text
    task = tasks(r)[0]
    source = task.params_json['submission']['volume_source']
    assert source == {'version': 1, 'project_id': str(r.pid), 'volume_id': str(r.session.get(Chapter, r.cid).volume_id), 'volume_idx': 1}
    r.session.get(Volume, r.session.get(Chapter, r.cid).volume_id).volume_idx = 3
    r.session.commit()
    assert (await submit_volume(r)).status_code == 200
    assert tasks(r)[0].params_json['submission']['volume_source'] == source


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['duplicate', 'forged'])
async def test_ambiguous_or_client_forged_volume_candidate_is_not_submitted(queued, invalid):
    r = queued
    changes = {}
    if invalid == 'duplicate':
        r.session.add(Volume(project_id=r.pid, volume_idx=1, title='同号另一卷')); r.session.commit()
    else: changes = {'params': {'volume_candidate': {'version': 1, 'text_hash': 'a' * 64}}}
    response = await submit_volume(r, **changes)
    assert response.status_code == 422 and not tasks(r)
    r.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing_source', 'params_changed', 'moved', 'duplicate', 'deleted'])
async def test_volume_worker_rechecks_submission_before_any_model_execution(worker, change):
    r = worker
    assert (await submit_volume(r)).status_code == 200
    task = tasks(r)[0]
    volume = r.session.get(Volume, r.session.get(Chapter, r.cid).volume_id)
    if change == 'missing_source': task.params_json = {**task.params_json, 'submission': {'version': 1}}
    elif change == 'params_changed': task.params_json = {**task.params_json, 'volume_idx': 2}
    elif change == 'moved': volume.volume_idx = 2
    elif change == 'duplicate': r.session.add(Volume(project_id=r.pid, volume_idx=1, title='重复卷号'))
    else: r.session.execute(delete(Volume).where(Volume.id == volume.id))
    r.session.commit()
    with pytest.raises(StaleChapterWrite):
        await r.run(str(task.id))
    r.implementation.assert_not_awaited()
    assert tasks(r)[0].status == 'failed'


@pytest.mark.asyncio
async def test_volume_worker_claims_repeated_delivery_only_once(worker):
    r = worker
    assert (await submit_volume(r)).status_code == 200
    await r.run(r.request['request_id'])
    await r.run(r.request['request_id'])
    assert r.implementation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'stream', 'moved'])
async def test_actual_volume_worker_keeps_complete_candidate_without_adopting_it(worker, monkeypatch, failure):
    from app.tasks import generation_tasks
    from app.services.outline_generator import OutlineGenerator
    r = worker
    assert (await submit_volume(r)).status_code == 200
    task = tasks(r)[0]
    volume_id = r.session.get(Chapter, r.cid).volume_id
    originals = {row.id: deepcopy(row.content_json) for row in r.session.query(Outline).all()}
    body = json.dumps({'summary': '当面交足两千元。', 'key_scene': '纸上写着 *两千*，旁边是 `已收`。'}, ensure_ascii=False)
    first = body[:len(body)//2]
    calls = []
    async def generate(_self, **kwargs):
        calls.append(kwargs)
        async def stream():
            yield first
            if failure == 'stream': raise RuntimeError('private upstream failure')
            yield body[len(first):]
            if failure == 'moved':
                def author_move():
                    with Session(r.engine) as other:
                        other.get(Volume, volume_id).volume_idx = 2
                        other.commit()
                Context().run(author_move)
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_volume_outline', generate)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    monkeypatch.setattr(generation_tasks, '_run_async_generation_impl', actual_implementation)
    await r.run(str(task.id))
    saved = tasks(r)[0]
    assert len(calls) == 1 and calls[0]['volume_idx'] == 1
    assert {row.id: row.content_json for row in r.session.query(Outline).all()} == originals
    if failure:
        if failure == 'moved':
            r.session.expire_all()
            assert r.session.get(Volume, volume_id).volume_idx == 2
        assert saved.status == 'failed' and 'private' not in saved.error_message
        assert saved.result_text == (first if failure == 'stream' else body)
        async with r.sessions() as db:
            with pytest.raises(VolumeOutlineError): await read_volume_candidate(db, r.pid, saved.id)
    else:
        assert saved.status == 'completed' and saved.result_text == body
        assert saved.params_json['volume_candidate']['text_hash'] == text_digest(body)
        async with r.sessions() as db:
            candidate = await read_volume_candidate(db, r.pid, saved.id)
        assert candidate['volume_id'] == str(volume_id) and candidate['text'] == body
