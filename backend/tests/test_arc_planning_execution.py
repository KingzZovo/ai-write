"""Interruptions, source edits and ambiguous atomic saves through real SQL."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.project import Chapter, Outline, Volume
from app.services import arc_planning_execution as execution
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count


@pytest.mark.asyncio
async def test_all_batches_and_delivery_are_complete_and_committed_once(planning_book):
    r = planning_book; body = await request_body(r)
    await submit(r, body)
    assert (await run(r, body["request_id"]))["status"] == "checked"
    state = snapshot(r, body["request_id"])
    assert state["status"] == "completed" and state["accepted_chapters"] == 11
    assert len(r.calls) == 4 and state["execution_attempts"] == 1
    receipt = state["result_json"]
    with Session(r.engine) as db:
        chapters = list(db.scalars(sa.select(Chapter).where(Chapter.volume_id == UUID(receipt["volume_id"])).order_by(Chapter.chapter_idx)))
        assert [str(c.id) for c in chapters] == receipt["chapter_ids"]
        assert [c.global_idx for c in chapters] == list(range(2, 13))
        assert all(c.content_text == "" and c.status == "draft" for c in chapters)
        assert db.get(Chapter, r.cid).content_text == "既有作者正文。"
        outline = db.get(Outline, UUID(receipt["outline_id"]))
        assert outline.content_json["_arc"]["input_material"]["background"] == body["background"]
    await run(r, body["request_id"], manual=True)
    assert snapshot(r, body["request_id"]) == state and len(r.calls) == 4 and volume_count(r) == 2


@pytest.mark.asyncio
async def test_interrupted_later_batch_keeps_accepted_plans_and_uses_original_route(planning_book):
    r = planning_book; body = await request_body(r)
    await submit(r, body)
    async def interrupt(task, user, db, kwargs):
        if "start=6, end=10" in user: raise asyncio.CancelledError()
    r.before_call = interrupt
    with pytest.raises(asyncio.CancelledError): await run(r, body["request_id"])
    first = snapshot(r, body["request_id"])
    assert first["accepted_chapters"] == 5 and volume_count(r) == 1
    await run(r, body["request_id"])  # A fresh duplicate is read-only.
    assert snapshot(r, body["request_id"]) == first
    r.before_call = None
    await run(r, body["request_id"], manual=True)
    final = snapshot(r, body["request_id"])
    assert final["status"] == "completed" and final["execution_attempts"] == 2
    assert final["journal_json"]["stages"][:2] == first["journal_json"]["stages"][:2]
    assert len(r.calls) == 5
    assert r.calls[2][2]["frozen_prompt"] == r.calls[3][2]["frozen_prompt"]
    assert r.calls[3][2]["temperature"] == 0.0
    assert len(r.route_calls) == 4


@pytest.mark.asyncio
async def test_interrupt_after_accepted_commit_does_not_regenerate_that_stage(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    r.worker_fault = {"condition": lambda row: row.phase == "chapters_1_4" and not row.accepted_chapters,
                      "when": "after", "cancel": True}
    with pytest.raises(asyncio.CancelledError): await run(r, body["request_id"])
    saved = snapshot(r, body["request_id"])
    assert saved["journal_json"]["stages"][0]["attempts"][-1]["state"] == "accepted"
    await run(r, body["request_id"], manual=True)
    assert snapshot(r, body["request_id"])["status"] == "completed"
    assert [task for task, _, _ in r.calls] == ["arc_outline_meta", "arc_outline_batch"]


@pytest.mark.asyncio
async def test_two_failed_calls_stop_later_stages_and_retries_cannot_reset_budget(planning_book):
    r = planning_book; body = await request_body(r)
    await submit(r, body)
    raw = '未完成的原输出。' * 1000
    async def fail(task, user, db, kwargs):
        if "start=6, end=10" in user: return NS(text=raw, finish_reason="length")
    r.before_call = fail
    await run(r, body["request_id"])
    state = snapshot(r, body["request_id"])
    assert state["status"] == "needs_review" and state["accepted_chapters"] == 5
    assert len(r.calls) == 4 and raw in r.calls[-1][1] and volume_count(r) == 1
    async with r.client() as client:
        assert (await client.post(path(r, f'/{body["request_id"]}/resume'))).status_code == 409
    await run(r, body["request_id"], manual=True)
    assert snapshot(r, body["request_id"]) == state and len(r.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before", "after"])
async def test_volume_chapters_outline_and_receipt_share_the_uncertain_final_commit(planning_book, fault):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    r.worker_fault = {"condition": lambda row: row.status == "completed", "when": fault}
    assert (await run(r, body["request_id"]))["status"] == "unconfirmed"
    state = snapshot(r, body["request_id"])
    assert (state["status"] == "completed") == (fault == "after")
    assert volume_count(r) == (2 if fault == "after" else 1)
    r.calls.clear()
    await run(r, body["request_id"], manual=True)
    final = snapshot(r, body["request_id"])
    assert final["status"] == "completed" and volume_count(r) == 2 and not r.calls
    assert final["journal_json"] == state["journal_json"]
    if fault == "after": assert final == state


@pytest.mark.asyncio
async def test_author_cancel_during_model_call_keeps_input_and_prevents_late_save(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    async def cancel(task, user, db, kwargs):
        async with r.client() as client:
            response = await client.post(path(r, f'/{body["request_id"]}/cancel'))
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
    r.before_call = cancel
    await run(r, body["request_id"])
    state = snapshot(r, body["request_id"])
    assert state["status"] == "cancelled" and state["request_json"]["background"] == body["background"]
    assert len(r.calls) == 1 and volume_count(r) == 1
    await run(r, body["request_id"], manual=True)
    assert snapshot(r, body["request_id"]) == state


@pytest.mark.asyncio
async def test_original_physical_connection_loss_cannot_write_the_late_output(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    async def lose(task, user, db, kwargs): db.guard.revoke()
    r.before_call = lose
    assert (await run(r, body["request_id"]))["status"] == "unconfirmed"
    state = snapshot(r, body["request_id"])
    assert state["journal_json"]["stages"][0]["attempts"][0]["state"] == "reserved"
    assert volume_count(r) == 1
    r.before_call = None
    await run(r, body["request_id"], manual=True)
    assert snapshot(r, body["request_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_actual_project_lock_excludes_duplicate_worker_and_author_structure_write(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    async def overlap(task, user, db, kwargs):
        assert (await run(r, body["request_id"], manual=True))["status"] == "busy"
        from app.services.chapter_order import ChapterOrderConflict, lock_chapter_structure
        async with r.sessions() as other:
            with pytest.raises(ChapterOrderConflict): await lock_chapter_structure(other, r.pid)
    r.before_call = overlap
    await run(r, body["request_id"])
    assert snapshot(r, body["request_id"])["status"] == "completed" and len(r.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["structure", "journal", "source", "request", "budget"])
async def test_interrupted_request_rechecks_originals_without_fabricating_recovery(planning_book, change):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    async def interrupt(*args): raise asyncio.CancelledError()
    r.before_call = interrupt
    with pytest.raises(asyncio.CancelledError): await run(r, body["request_id"])
    first = snapshot(r, body["request_id"])
    with r.engine.begin() as db:
        if change == "structure": db.execute(sa.update(Volume).where(Volume.id == r.vid).values(volume_idx=7))
        else:
            key = {"journal": "journal_json", "source": "source_json", "request": "request_json", "budget": "execution_history_json"}[change]
            db.execute(sa.update(Request).where(Request.id == UUID(body["request_id"])).values(**{key: [] if change == "budget" else {}}))
    corrupted = snapshot(r, body["request_id"])
    r.before_call = None
    await run(r, body["request_id"], manual=True)
    final = snapshot(r, body["request_id"])
    assert final["status"] == "needs_review" and len(r.calls) == 1 and volume_count(r) == 1
    for key in ("request_json", "source_json", "journal_json", "execution_history_json"):
        assert final[key] == corrupted[key]
    assert final["execution_attempts"] == first["execution_attempts"]


@pytest.mark.asyncio
async def test_audit_after_two_interrupted_calls_stops_without_a_third_paid_attempt(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    async def interrupt(*args): raise asyncio.CancelledError()
    r.before_call = interrupt
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError): await run(r, body["request_id"], manual=True)
    async with r.client() as client:
        result = await client.post(path(r, f'/{body["request_id"]}/resume'))
        assert result.status_code == 202  # Explicit audit is allowed; budget is not replenished.
    r.before_call = None
    await run(r, body["request_id"], manual=True)
    state = snapshot(r, body["request_id"])
    assert state["status"] == "needs_review" and state["error_code"] == "phase_budget_exhausted"
    assert len(r.calls) == 2 and state["execution_attempts"] == 2 and volume_count(r) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fully_planned", [False, True])
async def test_three_execution_limit_preserves_accepted_work_and_still_allows_atomic_save(fully_planned, planning_book):
    r = planning_book; body = await request_body(r)
    await submit(r, body)
    phases = ["chapters_1_5", "chapters_6_10", "finalize" if fully_planned else "chapters_11_11"]
    for phase in phases:
        r.worker_fault = {"condition": lambda row, phase=phase: row.phase == phase, "when": "after", "cancel": True}
        with pytest.raises(asyncio.CancelledError): await run(r, body["request_id"], manual=True)
    before = snapshot(r, body["request_id"])
    assert before["execution_attempts"] == 3
    count = len(r.calls)
    await run(r, body["request_id"], manual=True)
    after = snapshot(r, body["request_id"])
    assert after["journal_json"] == before["journal_json"] and len(r.calls) == count
    assert after["status"] == ("completed" if fully_planned else "needs_review")
    assert after["execution_attempts"] == 3
    assert volume_count(r) == (2 if fully_planned else 1)


@pytest.mark.asyncio
async def test_another_active_request_cannot_be_overlapped_by_resuming_an_old_paused_one(planning_book, monkeypatch):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    original = execution.capture_prompt_snapshot
    async def missing(*args): raise ValueError("unavailable route")
    monkeypatch.setattr(execution, "capture_prompt_snapshot", missing)
    await run(r, body["request_id"])
    before = snapshot(r, body["request_id"])
    assert before["error_code"] == "route_unavailable"
    other = await request_body(r, target=4)
    assert (await submit(r, other)).status_code == 202
    monkeypatch.setattr(execution, "capture_prompt_snapshot", original)
    assert (await run(r, body["request_id"], manual=True))["status"] == "busy"
    assert snapshot(r, body["request_id"]) == before and not r.calls


@pytest.mark.asyncio
async def test_original_registered_route_failure_recovers_without_changing_request(planning_book, monkeypatch):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    original = execution.capture_prompt_snapshot
    async def missing(*args): raise ValueError("unavailable route")
    monkeypatch.setattr(execution, "capture_prompt_snapshot", missing)
    await run(r, body["request_id"])
    before = snapshot(r, body["request_id"])
    monkeypatch.setattr(execution, "capture_prompt_snapshot", original)
    await run(r, body["request_id"], manual=True)
    after = snapshot(r, body["request_id"])
    assert after["status"] == "completed" and after["request_json"] == before["request_json"]
    assert after["execution_attempts"] == 2 and len(r.calls) == 2


@pytest.mark.asyncio
async def test_each_model_phase_has_a_timeout_and_does_not_launch_an_extra_retry(planning_book, monkeypatch):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body)
    monkeypatch.setattr(execution, "ARC_PHASE_TIMEOUT_SECONDS", 0.01)
    async def stall(*args): await asyncio.Event().wait()
    r.before_call = stall
    await run(r, body["request_id"])
    state = snapshot(r, body["request_id"])
    assert state["status"] == "needs_review" and len(r.calls) == 2
    attempts = state["journal_json"]["stages"][0]["attempts"]
    assert len(attempts) == 2 and all(attempt["error"] == "模型阶段超时" for attempt in attempts)
