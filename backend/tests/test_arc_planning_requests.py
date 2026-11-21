"""Fixed HTTP requests, original material, bounded histories and read-only replay."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.project import Chapter, Project, Volume
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count


@pytest.mark.asyncio
async def test_fixed_submission_is_committed_before_publish_and_exact_replay_is_read_only(planning_book):
    r = planning_book; body = await request_body(r)
    response = await submit(r, body)
    assert response.status_code == 202, response.text
    assert response.json()["target_volume_idx"] == 6 and volume_count(r) == 1
    state = snapshot(r, body["request_id"])
    repeated = await submit(r, body)
    assert repeated.status_code == 202 and repeated.json()["request_hash"] == response.json()["request_hash"]
    assert snapshot(r, body["request_id"]) == state
    assert r.published == [(body["request_id"], False)] and not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("idea", "另一段故事"), ("background", "新背景"),
    ("core_setup", "改设定"), ("opening_scene", "另一起点"), ("target_chapters", 4), ("mode", "next")])
async def test_original_id_cannot_be_rebound_to_other_material(planning_book, field, value):
    r = planning_book; body = await request_body(r)
    assert (await submit(r, body)).status_code == 202
    response = await submit(r, {**body, field: value})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "arc_planning_request_conflict"
    assert snapshot(r, body["request_id"])["request_json"][field] == body[field]
    assert len(r.published) == 1


@pytest.mark.asyncio
async def test_legacy_unscoped_requests_cannot_bypass_persistence(planning_book):
    r = planning_book
    async with r.client() as client:
        for endpoint in ("start", "next-arc"):
            response = await client.post(f"/api/arc/{r.pid}/{endpoint}", json={"idea": "原请求"})
            assert response.status_code == 428, response.text
    assert not r.calls and not r.published and volume_count(r) == 1


@pytest.mark.asyncio
async def test_sources_and_active_request_are_checked_before_model_calls(planning_book):
    r = planning_book; body = await request_body(r)
    with r.engine.begin() as db:
        db.execute(sa.update(Volume).where(Volume.id == r.vid).values(volume_idx=9))
    assert (await submit(r, body)).status_code == 409
    assert snapshot(r, body["request_id"]) is None and not r.published
    body = await request_body(r)
    assert (await submit(r, body)).status_code == 202
    other = {**body, "request_id": str(uuid4())}
    response = await submit(r, other)
    assert response.status_code == 409 and response.json()["detail"]["request_id"] == body["request_id"]
    assert not r.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before", "after"])
async def test_unconfirmed_commit_keeps_the_original_uuid_and_does_not_publish(planning_book, fault):
    r = planning_book; body = await request_body(r)
    r.http_fault = fault
    response = await submit(r, body)
    assert response.status_code == 503 and "private" not in response.text
    assert response.json()["detail"]["request_id"] == body["request_id"]
    assert (snapshot(r, body["request_id"]) is not None) == (fault == "after")
    assert not r.published
    assert (await submit(r, body)).status_code == 202
    with Session(r.engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Request)) == 1


@pytest.mark.asyncio
async def test_cross_project_reads_and_commands_never_return_original_material(planning_book):
    r = planning_book; body = await request_body(r)
    assert (await submit(r, body)).status_code == 202
    async with r.client() as client:
        for suffix in ("", "/detail", "/resume", "/cancel"):
            url = f'/api/projects/{r.foreign}/arc-plans/{body["request_id"]}{suffix}'
            result = await (client.post(url) if suffix in ("/resume", "/cancel") else client.get(url))
            assert result.status_code == 404 and body["background"] not in result.text
        for field in ("request_id", "expected_source"):
            broken = deepcopy(body); broken.pop(field)
            assert (await client.post(path(r), json=broken)).status_code == 428


@pytest.mark.asyncio
async def test_original_receipt_survives_volume_deletion_and_never_recreates_it(planning_book):
    r = planning_book; body = await request_body(r, target=4)
    await submit(r, body); await run(r, body["request_id"])
    original = snapshot(r, body["request_id"])
    assert original["status"] == "completed", original["error_message"]
    with r.engine.begin() as db:
        db.execute(sa.delete(Volume).where(Volume.id == UUID(original["result_json"]["volume_id"])))
    r.calls.clear()
    response = await submit(r, body)
    assert response.status_code == 202 and response.json()["result"] == original["result_json"]
    await run(r, body["request_id"], manual=True)
    assert volume_count(r) == 1 and not r.calls
    assert snapshot(r, body["request_id"]) == original
    with r.engine.begin() as db: db.execute(sa.delete(Project).where(Project.id == r.pid))
    assert snapshot(r, body["request_id"]) is None


@pytest.mark.asyncio
async def test_progress_poll_and_history_do_not_read_full_prompts_or_prose(planning_book):
    r = planning_book; body = await request_body(r)
    await submit(r, body)
    r.queries.clear()
    async with r.client() as client:
        status = await client.get(path(r, f'/{body["request_id"]}'))
        history = await client.get(path(r))
    assert status.status_code == history.status_code == 200
    assert all(all(field not in query for field in ("request_json", "journal_json", "source_json", "content_text")) for query in r.queries)
    state = snapshot(r, body["request_id"])
    with r.engine.begin() as db:
        db.execute(sa.update(Project).where(Project.id == r.pid).values(deleted_at=datetime.now(timezone.utc)))
    async with r.client() as client:
        assert (await client.get(path(r, f'/{body["request_id"]}/detail'))).status_code == 404
    assert snapshot(r, body["request_id"]) == state


@pytest.mark.asyncio
async def test_actual_http_contract_fixture_is_complete(planning_book):
    import json
    from pathlib import Path
    r = planning_book; body = await request_body(r, target=4)
    pending = await submit(r, body)
    async with r.client() as client:
        before = await client.get(path(r, f'/{body["request_id"]}/detail'))
    await run(r, body["request_id"])
    async with r.client() as client:
        after = await client.get(path(r, f'/{body["request_id"]}/detail'))
    assert before.status_code == after.status_code == 200
    assert before.json()["integrity_valid"] and after.json()["integrity_valid"]
    assert after.json()["status"] == "completed" and len(after.json()["accepted_outline"]["chapter_summaries"]) == 4
    Path('/tmp/aiwrite-arc-plan-contract-20261009.json').write_text(json.dumps(
        {"input": body, "pending": pending.json(), "before": before.json(), "after": after.json()}, ensure_ascii=False, indent=2))


@pytest.mark.asyncio
async def test_completed_progress_is_small_even_when_original_author_material_is_long(planning_book):
    r = planning_book
    setup, opening = "作者的完整关系设定。" * 10000, "作者指定的开场情境。" * 10000
    body = await request_body(r, target=4, core_setup=setup, opening_scene=opening)
    await submit(r, body); await run(r, body["request_id"])
    async with r.client() as client:
        response = await client.get(path(r, f'/{body["request_id"]}'))
        detail = await client.get(path(r, f'/{body["request_id"]}/detail'))
    assert response.json()["status"] == "completed" and len(response.content) < 10_000
    assert setup not in response.text and opening not in response.text
    assert detail.json()["request"]["core_setup"] == setup and detail.json()["request"]["opening_scene"] == opening


@pytest.mark.asyncio
async def test_history_cursor_pages_every_request_once_and_rejects_unknown_cursor(planning_book):
    r = planning_book; identities = []
    for _ in range(5):
        body = await request_body(r, target=4); identities.append(body["request_id"])
        await submit(r, body)
        async with r.client() as client: assert (await client.post(path(r, f'/{body["request_id"]}/cancel'))).status_code == 200
    seen, cursor = [], None
    async with r.client() as client:
        while True:
            page = await client.get(path(r) + '?limit=2' + (f'&cursor={cursor}' if cursor else ''))
            assert page.status_code == 200
            assert not set(item["request_id"] for item in page.json()["items"]).intersection(seen), "history cursor repeated a page"
            seen.extend(item["request_id"] for item in page.json()["items"])
            cursor = page.json()["next_cursor"]
            if cursor is None: break
        assert (await client.get(path(r) + f'?cursor={uuid4()}')).status_code == 422
    assert len(seen) == len(set(seen)) == 5 and set(seen) == set(identities)
