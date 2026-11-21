"""Actual generation branches distinguish operation status from retained prose."""
import asyncio

import pytest

from app.services.generation_task_recovery import STATUS_PROGRESS
from tests.test_generation_task_execution_core import chapter_task, core_task
from tests.test_generation_task_execution import DELIVERED, ORIGINAL, body, run, snapshot, submit, task_book
from tests.test_generation_task_recovery import expire, recover


@pytest.mark.asyncio
async def test_direct_model_interruption_can_resume_after_a_typed_operation_message(core_task):
    r = core_task
    identity = chapter_task(r)
    r.model.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await run(identity)
    expire(r, identity)
    before = snapshot(r, identity)
    assert before['progress_kind'] == 'status' and before['progress_text'] in STATUS_PROGRESS
    assert before['char_count'] == 0 and body(r) == ORIGINAL
    r.model.side_effect = None
    await recover(identity)
    state = snapshot(r, identity)
    assert state['status'] == 'completed' and state['attempt_count'] == 2
    assert state['progress_kind'] == 'candidate' and body(r) == DELIVERED
    assert state['saved_manuscript_json']['execution_id'] == str(state['execution_id'])
    assert r.model.await_count == 2 and r.evaluate.await_count == 1


@pytest.mark.asyncio
async def test_evaluation_interruption_keeps_returned_prose_and_never_generates_again(core_task):
    r = core_task
    identity = chapter_task(r)
    r.evaluate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await run(identity)
    expire(r, identity)
    before = snapshot(r, identity)
    assert before['status'] == 'evaluating' and before['progress_kind'] == 'candidate'
    assert before['progress_text'] == DELIVERED and before['saved_manuscript_json'] is None
    await recover(identity)
    after = snapshot(r, identity)
    assert after['status'] == 'needs_review' and after['progress_text'] == DELIVERED
    assert after['attempt_count'] == 1 and body(r) == ORIGINAL
    assert after['recovery_history_json'][-1]['outcome'] == 'candidate_requires_review'
    r.model.assert_awaited_once()
    r.evaluate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_chunk', ['', '\n  ', {'event': 'heartbeat'}])
async def test_empty_stream_control_chunk_does_not_create_a_retained_candidate(core_task, monkeypatch, empty_chunk):
    r = core_task
    identity = submit(r, auto_revise=False)
    monkeypatch.setattr('app.config.settings.FORCE_DIRECT_CHAPTER', False)
    async def interrupted(*a, **k):
        yield empty_chunk
        raise asyncio.CancelledError()
    monkeypatch.setattr('app.services.scene_orchestrator.SceneOrchestrator.orchestrate_chapter_stream', interrupted)
    with pytest.raises(asyncio.CancelledError): await run(identity)
    expire(r, identity)
    before = snapshot(r, identity)
    assert before['progress_text'] == '' and before['progress_kind'] is None
    assert before['char_count'] == 0 and body(r) == ORIGINAL
    async def successful(*a, **k): yield DELIVERED
    monkeypatch.setattr('app.services.scene_orchestrator.SceneOrchestrator.orchestrate_chapter_stream', successful)
    await recover(identity)
    after = snapshot(r, identity)
    assert after['status'] == 'completed' and after['attempt_count'] == 2
    assert after['progress_kind'] == 'candidate' and body(r) == DELIVERED
