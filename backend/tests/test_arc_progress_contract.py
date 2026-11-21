"""Reject incomplete and mismatched decisions before any write or model call."""
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.arc_progress import ArcProgressBody, write_arc_progress
from app.services.arc_progress_contract import ArcProgressError, request_intent
from tests.support.arc_progress import chapter_ids, confirmation_request, save_manuscript
from tests.test_arc_persistence import create_arc
from tests.test_chapter_order_maintenance import book


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["request_id", "expected_source", "expected_content_text", "expected_title"])
async def test_new_interface_requires_fixed_request_source_and_full_manuscript(book, field):
    _, volume, _, _ = await create_arc(book)
    cid = chapter_ids(book, volume)[0]
    save_manuscript(book, cid)
    body = await confirmation_request(book, volume, cid)
    setattr(body, field, None)
    with pytest.raises(HTTPException) as exc: await write_arc_progress(book.pid, volume.id, body, book.db)
    assert exc.value.status_code == 428


@pytest.mark.parametrize("change", ["uuid", "chapter_id", "source_schema", "nonfinite", "action", "title", "note_bool",
    "note_array", "direction_for_ack", "closing_for_ack", "no_chapter", "extra", "empty_direction", "wrong_chapter_action"])
def test_contract_rejects_malformed_or_ambiguous_requests(change):
    pid, vid = uuid4(), uuid4()
    request = dict(request_id=str(uuid4()), expected_source={"schema": 1}, action="acknowledge",
                   chapter_id=str(uuid4()), expected_content_text="正文全文。", expected_title="归还", author_note="")
    if change == "uuid": request["request_id"] = "unknown"
    elif change == "chapter_id": request["chapter_id"] = False
    elif change == "source_schema": request["expected_source"] = {"schema": True}
    elif change == "nonfinite": request["expected_source"]["number"] = float("inf")
    elif change == "action": request["action"] = ["acknowledge"]
    elif change == "title": request["expected_title"] = 123
    elif change == "note_bool": request["author_note"] = False
    elif change == "note_array": request["author_note"] = []
    elif change == "direction_for_ack": request["direction"] = "另一操作"
    elif change == "closing_for_ack": request["closing_note"] = "另一操作"
    elif change == "no_chapter": request["chapter_id"] = None
    elif change == "extra": request["unknown"] = "不可丢掉的作者输入"
    elif change == "empty_direction": request = dict(request_id=str(uuid4()), expected_source={"schema": 1}, action="direction", direction="\n ")
    else: request["action"] = "finish"
    with pytest.raises(ArcProgressError) as exc: request_intent(pid, vid, request)
    assert exc.value.status in (422, 428)


def test_request_defaults_are_stable_without_changing_the_callers_input():
    values = dict(request_id=str(uuid4()), expected_source={"schema": 1}, action="direction", direction="继续")
    original = deepcopy(values)
    pid, vid = uuid4(), uuid4()
    first = request_intent(pid, vid, values)
    assert first[3] == request_intent(pid, vid, ArcProgressBody(**values).model_dump())[3]
    assert values == original
