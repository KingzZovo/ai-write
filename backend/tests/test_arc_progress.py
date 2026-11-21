"""Actual SQLite transactions and ASGI arc decisions, without service I/O."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4
import json
from pathlib import Path

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, func, insert, select, update

from app.api import arc as arc_api, arc_progress as api
from app.models.arc_progress import ArcProgressWrite
from app.models.project import Chapter, Outline, Project, Volume
from app.services.arc_progress_contract import ArcProgressError, text_hash
from app.services.arc_progress_reader import load_arc_progress
from app.services.arc_progress_writes import prepare_arc_progress_write
from app.services.entity_sources import digest
from tests.support.arc_progress import acknowledge, chapter_ids, confirmation_request, request, save_manuscript, submit
from tests.test_arc_persistence import create_arc
from tests.test_chapter_order_maintenance import book, move


def history_count(r):
    return r.session.scalar(select(func.count()).select_from(ArcProgressWrite))


async def setup(r):
    _, volume, outline, _ = await create_arc(r)
    return volume, outline, chapter_ids(r, volume)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " \n\t\u3000"])
async def test_empty_saved_manuscript_cannot_count_as_written(book, text):
    volume, outline, ids = await setup(book)
    original = deepcopy(outline.content_json)
    save_manuscript(book, ids[0], text)
    with pytest.raises(HTTPException) as exc:
        await acknowledge(book, volume, ids[0])
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "arc_manuscript_empty"
    assert history_count(book) == 0 and outline.content_json == original
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "needs_review", "completed"])
async def test_author_can_confirm_the_complete_saved_manuscript_without_a_model_verdict(book, status):
    volume, outline, ids = await setup(book)
    original = deepcopy(outline.content_json)
    full = "林禾把信封放在桌上，里面是三千元。\n" * 2000
    save_manuscript(book, ids[1], full, status=status)
    body = await confirmation_request(book, volume, ids[1], author_note="这一章的交款已写到结果。")
    async with book.client() as client:
        response = await client.post(f"/api/projects/{book.pid}/arc-progress/{volume.id}", json=body.model_dump())
        assert response.status_code == 200, response.text
    result = response.json()
    assert result["sequence"] == 1
    row = book.session.get(ArcProgressWrite, UUID(body.request_id))
    assert row.manuscript_json["content_text"] == full
    assert row.manuscript_hash == text_hash(full)
    assert row.before_json["legacy"] == original["_arc"]
    assert row.result_hash == digest(result)
    view = (await load_arc_progress(book.db, book.pid, volume.id)).public()
    assert view["confirmed_count"] == 1 and view["next_chapter_id"] == str(ids[0])
    assert view["chapters"][1]["confirmation"]["author_note"] == body.author_note
    chapter = book.session.get(Chapter, ids[1])
    assert chapter.status == status and chapter.content_text == full
    assert outline.content_json == original
    current = await arc_api.current_arc(str(book.pid), book.db)
    assert current["arc"]["chapters_written"] == 1
    brief = await arc_api.chapter_brief(str(book.pid), book.db)
    assert brief["chapter_id"] == str(ids[0]) and brief["next_chapter_idx"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expected_content_text", "expected_title"])
async def test_confirmation_compares_actual_full_title_and_prose(book, field):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0], "前段" * 5000 + "信封内仍有一千元。")
    body = await confirmation_request(book, volume, ids[0])
    setattr(body, field, getattr(body, field) + "改动")
    with pytest.raises(HTTPException) as exc:
        await api.write_arc_progress(book.pid, volume.id, body, book.db)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "arc_manuscript_changed"
    assert history_count(book) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fail_commit", "lose_ack"])
async def test_commit_failure_or_lost_ack_keeps_original_request_and_never_double_advances(book, failure):
    volume, _, ids = await setup(book)
    vid = volume.id
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    setattr(book, failure, True)
    async with book.client() as client:
        url = f"/api/projects/{book.pid}/arc-progress/{vid}"
        first = await client.post(url, json=body.model_dump())
        assert first.status_code == 503 and first.json()["detail"]["request_id"] == body.request_id
        assert "private" not in first.text
        assert history_count(book) == (1 if failure == "lose_ack" else 0)
        retry = await client.post(url, json=body.model_dump())
        assert retry.status_code == 200, retry.text
        again = await client.post(url, json=body.model_dump())
        assert again.json() == retry.json()
    assert history_count(book) == 1
    view = (await load_arc_progress(book.db, book.pid, vid)).public()
    assert view["confirmed_count"] == 1 and len(view["progress"]["acknowledged"]) == 1


@pytest.mark.asyncio
async def test_unknown_commit_retry_after_later_edit_reads_the_original_receipt(book):
    volume, _, ids = await setup(book)
    vid = volume.id
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    original = await api.write_arc_progress(book.pid, vid, body, book.db)
    save_manuscript(book, ids[0], "作者改稿，留在宋岚手中的书尚未归还。")
    book.queries.clear()
    assert await api.write_arc_progress(book.pid, vid, body, book.db) == original
    assert not any("chapters.content_text" in str(q) for q in book.queries)
    current = await load_arc_progress(book.db, book.pid, vid)
    assert current.public()["confirmed_count"] == 0
    assert current.chapters[0]["state"] == "changed"
    assert history_count(book) == 1
    with pytest.raises(HTTPException) as exc:
        await arc_api.chapter_brief(str(book.pid), book.db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["note", "source", "chapter", "text", "action", "volume", "project"])
async def test_uuid_cannot_be_reused_for_a_different_author_intent(book, change):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    await api.write_arc_progress(book.pid, volume.id, body, book.db)
    changed = body.model_copy(deep=True)
    pid, vid = book.pid, volume.id
    if change == "note": changed.author_note = "不同说明"
    elif change == "source": changed.expected_source = (await load_arc_progress(book.db, pid, vid)).source
    elif change == "chapter": changed.chapter_id = str(ids[1])
    elif change == "text": changed.expected_content_text += "又写了一句。"
    elif change == "action": changed.action, changed.expected_content_text, changed.expected_title = "retract", None, None
    elif change == "volume": vid = book.volumes[0]
    else: pid = book.foreign
    with pytest.raises(HTTPException) as exc:
        await api.write_arc_progress(pid, vid, changed, book.db)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "arc_progress_request_conflict"
    assert history_count(book) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plan", "version", "replacement", "unconfirm", "reorder", "delete_outline", "volume_label", "title"])
async def test_source_changes_cannot_be_accepted_with_a_stale_request(book, change):
    volume, outline, ids = await setup(book)
    vid = volume.id
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    if change == "plan": outline.content_json = {**outline.content_json, "author_extra": "新增的章末安排"}
    elif change == "version": outline.version += 1
    elif change == "replacement": book.session.add(Outline(project_id=book.pid, level="volume", is_confirmed=1,
        version=outline.version+1, content_json=deepcopy(outline.content_json)))
    elif change == "unconfirm": outline.is_confirmed = 0
    elif change == "reorder": book.session.execute(update(Chapter).where(Chapter.id == ids[0]).values(chapter_idx=9))
    elif change == "delete_outline": book.session.delete(outline)
    elif change == "volume_label": volume.volume_idx = 4
    else: book.session.execute(update(Chapter).where(Chapter.id == ids[1]).values(title="新的章名"))
    book.session.commit()
    with pytest.raises(HTTPException) as exc:
        await api.write_arc_progress(book.pid, vid, body, book.db)
    assert exc.value.status_code in (404, 409)
    assert history_count(book) == 0


@pytest.mark.asyncio
async def test_reorder_preserves_confirmations_by_identity_and_requires_explicit_reconciliation(book):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    first = await acknowledge(book, volume, ids[0])
    url, payload = move(book, ids[0], ids[1])
    async with book.client() as client:
        assert (await client.post(url, json=payload)).status_code == 200
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.public()["next_chapter_id"] is None
    assert {issue["code"] for issue in view.issues} >= {"order_changed", "plan_changed"}
    with pytest.raises(HTTPException):
        await submit(book, volume, "direction", direction="接着归还旧书")
    second = await submit(book, volume, "reconcile")
    assert second["progress"]["acknowledged"] == first["progress"]["acknowledged"]
    assert second["progress"]["chapter_order"] == [str(ids[1]), str(ids[0]), *map(str, ids[2:])]
    brief = await arc_api.chapter_brief(str(book.pid), book.db)
    assert brief["chapter_id"] == str(ids[1]) and brief["next_chapter_idx"] == 1
    assert history_count(book) == 2


@pytest.mark.asyncio
async def test_changed_manuscript_must_be_rechecked_not_erased_by_reconciliation(book):
    volume, outline, ids = await setup(book)
    save_manuscript(book, ids[0])
    old = await acknowledge(book, volume, ids[0])
    save_manuscript(book, ids[0], "林禾没有收回书，约好下个月再来。")
    outline.content_json = {**outline.content_json, "author_extra": "推迟还书"}
    book.session.commit()
    with pytest.raises(HTTPException): await submit(book, volume, "reconcile")
    checked = await acknowledge(book, volume, ids[0], author_note="已重读改稿")
    assert checked["progress"]["acknowledged"][str(ids[0])]["content_hash"] != old["manuscript_hash"]
    assert any(issue["code"] == "plan_changed" for issue in (await load_arc_progress(book.db, book.pid, volume.id)).issues)
    await submit(book, volume, "reconcile")
    assert (await load_arc_progress(book.db, book.pid, volume.id)).issues == []
    assert book.session.get(ArcProgressWrite, UUID(old["request_id"])).manuscript_json["content_text"] == "宋岚将旧书合上，交回林禾手中。"


@pytest.mark.asyncio
async def test_deleted_confirmed_chapter_requires_explicit_retraction_and_keeps_full_history(book):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    original = await acknowledge(book, volume, ids[0])
    async with book.client() as client:
        assert (await client.delete(f"/api/projects/{book.pid}/chapters/{ids[0]}")).status_code == 204
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.chapters[-1]["state"] == "removed" and view.public()["confirmed_count"] == 0
    with pytest.raises(HTTPException): await submit(book, volume, "reconcile")
    await submit(book, volume, "retract", chapter_id=str(ids[0]), author_note="合并到其他章节")
    await submit(book, volume, "reconcile")
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.issues == [] and view.public()["next_chapter_id"] == str(ids[1])
    detail = await api.arc_progress_detail(book.pid, volume.id, UUID(original["request_id"]), book.db)
    assert detail["integrity_valid"] and detail["manuscript"]["content_text"]
    assert history_count(book) == 3


@pytest.mark.asyncio
async def test_finishing_early_and_reopening_never_fabricate_written_chapters(book):
    volume, _, ids = await setup(book)
    ended = await submit(book, volume, "finish", closing_note="作者决定在这里收束本段故事。")
    assert ended["progress"]["closed_early"] and ended["progress"]["acknowledged"] == {}
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 0
    save_manuscript(book, ids[0])
    with pytest.raises(HTTPException) as exc: await acknowledge(book, volume, ids[0])
    assert exc.value.detail["code"] == "arc_progress_closed"
    with pytest.raises(HTTPException): await arc_api.chapter_brief(str(book.pid), book.db)
    await submit(book, volume, "reopen")
    await acknowledge(book, volume, ids[0])
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.public()["confirmed_count"] == 1 and view.public()["next_chapter_id"] == str(ids[1])
    assert view.progress["closed_early"] is False


@pytest.mark.asyncio
async def test_all_chapters_require_their_own_saved_confirmation_and_reopening_does_not_invent_a_fifth(book):
    volume, _, ids = await setup(book)
    for cid in reversed(ids):
        save_manuscript(book, cid)
        result = await acknowledge(book, volume, cid)
    assert result["progress"]["phase"] == "completed" and not result["progress"]["closed_early"]
    assert len(result["progress"]["acknowledged"]) == 4
    await submit(book, volume, "reopen")
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.public()["next_chapter_id"] is None and view.public()["confirmed_count"] == 4
    with pytest.raises(HTTPException): await arc_api.chapter_brief(str(book.pid), book.db)
    await submit(book, volume, "retract", chapter_id=str(ids[2]))
    assert (await arc_api.chapter_brief(str(book.pid), book.db))["chapter_id"] == str(ids[2])


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [{"chapters_written": 2}, {"status": "completed"}, {"running_outline": "旧报告全文。" * 3000}])
async def test_legacy_reports_are_visible_but_never_count_as_saved_manuscript_proofs(book, legacy):
    volume, outline, ids = await setup(book)
    outline.content_json = {**outline.content_json, "_arc": {**outline.content_json["_arc"], **legacy}}
    book.session.commit()
    raw = deepcopy(outline.content_json["_arc"])
    view = (await load_arc_progress(book.db, book.pid, volume.id)).public()
    assert view["legacy"]["unverified"] and view["confirmed_count"] == 0 and view["next_chapter_id"] is None
    save_manuscript(book, ids[0])
    with pytest.raises(HTTPException): await acknowledge(book, volume, ids[0])
    with pytest.raises(HTTPException): await submit(book, volume, "finish")
    adopted = await submit(book, volume, "reconcile")
    first_row = book.session.get(ArcProgressWrite, UUID(adopted["request_id"]))
    assert first_row.before_json["legacy"] == raw
    checked = await acknowledge(book, volume, ids[0])
    second_row = book.session.get(ArcProgressWrite, UUID(checked["request_id"]))
    assert "legacy" not in second_row.before_json
    assert checked["progress"]["legacy_request_id"] == adopted["request_id"]
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 1


@pytest.mark.asyncio
async def test_paginated_history_survives_volume_deletion_but_not_permanent_book_deletion(book):
    volume, _, ids = await setup(book)
    vid = volume.id
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    original = await api.write_arc_progress(book.pid, vid, body, book.db)
    for number in range(5): await submit(book, volume, "direction", direction=f"接下来第{number}种安排")
    async with book.client() as client:
        assert (await client.delete(f"/api/projects/{book.pid}/volumes/{vid}")).status_code == 204
        base = f"/api/projects/{book.pid}/arc-progress/{vid}"
        assert (await client.get(base)).status_code == 404
        book.queries.clear()
        page1 = (await client.get(base + "/history?limit=3")).json()
        page2 = (await client.get(base + "/history?limit=3&cursor=" + page1["next_cursor"])).json()
        assert [row["sequence"] for row in page1["items"] + page2["items"]] == [6, 5, 4, 3, 2, 1]
        assert page2["next_cursor"] is None
        assert not any("manuscript_json" in str(q) or "before_json" in str(q) for q in book.queries)
        detail = (await client.get(base + "/history/" + body.request_id)).json()
        assert detail["integrity_valid"] and detail["receipt"] == original
        assert (await client.post(base, json=body.model_dump())).json() == original
        catalog = (await client.get(f"/api/projects/{book.pid}/arc-progress")).json()
        assert catalog["items"] == [{"volume_id": str(vid), "title": original["volume_title"], "deleted": True}]
        assert (await client.get(base + "/history?cursor=" + str(uuid4()))).status_code == 422
        assert (await client.get(base.replace(str(book.pid), str(book.foreign)) + "/history/" + body.request_id)).status_code == 404
    book.session.execute(delete(Project).where(Project.id == book.pid)); book.session.commit()
    assert history_count(book) == 0


@pytest.mark.asyncio
async def test_soft_deleted_project_does_not_expose_or_replay_saved_decisions(book):
    volume, _, ids = await setup(book)
    vid = volume.id
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    await api.write_arc_progress(book.pid, vid, body, book.db)
    book.session.execute(update(Project).where(Project.id == book.pid).values(deleted_at=datetime.now(timezone.utc)))
    book.session.commit()
    async with book.client() as client:
        base = f"/api/projects/{book.pid}/arc-progress/{vid}"
        assert (await client.get(base)).status_code == 404
        assert (await client.get(base + "/history")).status_code == 404
        assert (await client.get(base + "/history/" + body.request_id)).status_code == 404
        assert (await client.post(base, json=body.model_dump())).status_code == 404
    assert history_count(book) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["result", "hash", "foreign_proof", "missing_proof", "missing_legacy", "phase", "bool_sequence"])
async def test_bad_latest_progress_never_falls_back_to_legacy_counts(book, corruption):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    first = await acknowledge(book, volume, ids[0])
    later = await submit(book, volume, "direction", direction="继续")
    row = book.session.get(ArcProgressWrite, UUID(later["request_id"]))
    result = deepcopy(row.result_json)
    if corruption == "result": result["progress"]["acknowledged"] = []
    elif corruption == "hash": row.result_hash = "0" * 64
    elif corruption == "foreign_proof": result["progress"]["acknowledged"][str(ids[0])]["request_id"] = later["request_id"]
    elif corruption == "missing_proof": book.session.execute(delete(ArcProgressWrite).where(ArcProgressWrite.id == UUID(first["request_id"])))
    elif corruption == "missing_legacy": result["progress"]["legacy_request_id"] = str(uuid4())
    elif corruption == "phase": result["progress"]["phase"] = "made_up"
    else: result["sequence"] = True
    if corruption not in ("hash", "missing_proof"):
        row.result_json = result
        row.result_hash = digest(result)
    book.session.commit()
    with pytest.raises(ArcProgressError): await load_arc_progress(book.db, book.pid, volume.id)
    with pytest.raises(HTTPException): await arc_api.current_arc(str(book.pid), book.db)


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["manuscript_json", "before_json"])
async def test_corrupted_historical_snapshots_remain_readable_without_false_integrity_claim(book, part):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    original = await acknowledge(book, volume, ids[0])
    row = book.session.get(ArcProgressWrite, UUID(original["request_id"]))
    setattr(row, part, {"damaged": True}); book.session.commit()
    detail = await api.arc_progress_detail(book.pid, volume.id, row.id, book.db)
    assert detail["integrity_valid"] is False
    assert detail["manuscript" if part == "manuscript_json" else "before"] == {"damaged": True}


@pytest.mark.asyncio
async def test_large_book_reads_current_arc_headers_and_only_needed_unknown_manuscripts(book):
    volume, _, ids = await setup(book)
    text = "前章的完整正文一直保留。" * 420
    count = 996
    assert len(text) * count >= 5_000_000
    book.session.execute(update(Project).where(Project.id == book.pid).values(target_word_count=5_000_000))
    book.session.execute(insert(Chapter), [{"id": uuid4(), "volume_id": book.volumes[1], "chapter_idx": i+100,
        "global_idx": i+100, "content_text": text, "title": f"远处旧章{i}"} for i in range(count)])
    book.session.commit()
    save_manuscript(book, ids[0])
    await acknowledge(book, volume, ids[0])
    book.queries.clear()
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.public()["confirmed_count"] == 1
    prose_reads = [q for q in book.queries if "chapters.content_text" in str(q)]
    assert len(prose_reads) == 1 and ids[0] in next(v for v in prose_reads[0].compile().params.values() if isinstance(v, list))
    assert not any("manuscript_json" in str(q) or "before_json" in str(q) for q in book.queries)
    book.session.execute(update(Chapter).where(Chapter.id == ids[0]).values(content_hash=view.chapters[0]["content_hash"]))
    book.session.commit(); book.queries.clear()
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 1
    assert not any("chapters.content_text" in str(q) or "manuscript_json" in str(q) for q in book.queries)
    outlines = [q for q in book.queries if any(getattr(c, "table", None) is Outline.__table__ and c.key == "content_json"
        for c in getattr(q, "selected_columns", []))]
    assert len(outlines) == 1


@pytest.mark.asyncio
async def test_prepared_progress_requires_the_callers_commit_and_rolls_back_together(book):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0])
    body = await confirmation_request(book, volume, ids[0])
    await prepare_arc_progress_write(book.db, book.pid, volume.id, **body.model_dump())
    assert history_count(book) == 1
    await book.db.rollback()
    assert history_count(book) == 0
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_archive_catalog_paginates_distinct_volumes_and_reads_no_manuscripts(book):
    volume, _, _ = await setup(book)
    _, other, _, _ = await create_arc(book, index=4)
    for item in (volume, other):
        await submit(book, item, "direction", direction="只写当前待写章。")
        await submit(book, item, "direction", direction="修改这份方向。")
    book.queries.clear()
    async with book.client() as client:
        base = f"/api/projects/{book.pid}/arc-progress"
        first = (await client.get(base + "?limit=1")).json()
        second = (await client.get(base + "?limit=1&cursor=" + first["next_cursor"])).json()
        assert {row["volume_id"] for row in first["items"] + second["items"]} == {str(volume.id), str(other.id)}
        assert second["next_cursor"] is None
        assert (await client.get(base.replace(str(book.pid), str(book.foreign)))).json()["items"] == []
    assert not any("manuscript_json" in str(q) or "before_json" in str(q) or "chapters.content_text" in str(q) for q in book.queries)


@pytest.mark.asyncio
async def test_capture_real_http_contract_for_frontend_review(book):
    volume, _, ids = await setup(book)
    save_manuscript(book, ids[0], "宋岚点清三千元，收下两千元，把剩下的一千元连同信封交回林禾。")
    body = await confirmation_request(book, volume, ids[0], author_note="款项及信封去向已核对。")
    async with book.client() as client:
        base = f"/api/projects/{book.pid}/arc-progress/{volume.id}"
        before = (await client.get(base)).json()
        written = await client.post(base, json=body.model_dump())
        assert written.status_code == 200
        after = (await client.get(base)).json()
        detail = (await client.get(base + "/history/" + body.request_id)).json()
    assert detail["receipt"] == written.json() and detail["integrity_valid"] is True
    assert after["confirmed_count"] == 1
    # Synthetic SQLite fiction only; this artifact contains no live credentials
    # or user manuscripts. Frontend tests consume its exact HTTP shapes.
    Path("/tmp/aiwrite-arc-progress-20261009-contract.json").write_text(json.dumps({
        "before": before, "after": after, "input": body.model_dump(), "receipt": written.json(), "detail": detail,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_missing_members_before_first_confirmation_require_explicit_adoption(book):
    volume, _, ids = await setup(book)
    async with book.client() as client:
        assert (await client.delete(f"/api/projects/{book.pid}/chapters/{ids[-1]}")).status_code == 204
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert any(issue["code"] == "order_changed" for issue in view.issues)
    assert view.progress["chapter_order"] == list(map(str, ids))
    save_manuscript(book, ids[0])
    with pytest.raises(HTTPException): await acknowledge(book, volume, ids[0])
    adopted = await submit(book, volume, "reconcile")
    assert adopted["progress"]["chapter_order"] == list(map(str, ids[:-1]))
    assert book.session.get(ArcProgressWrite, UUID(adopted["request_id"])).before_json["progress"]["chapter_order"] == list(map(str, ids))
    await acknowledge(book, volume, ids[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["early", "all_confirmed"])
async def test_revised_plan_needs_explicit_closure_even_if_the_old_arc_had_ended(book, close):
    volume, outline, ids = await setup(book)
    if close == "all_confirmed":
        for cid in ids:
            save_manuscript(book, cid); await acknowledge(book, volume, cid)
    else: await submit(book, volume, "finish", closing_note="原结束决定")
    outline.content_json = {**outline.content_json, "author_extra": "作者修改了这一段的去向"}
    book.session.commit()
    await submit(book, volume, "reconcile")
    view = await load_arc_progress(book.db, book.pid, volume.id)
    assert view.progress["phase"] == "completed"
    assert [issue["code"] for issue in view.issues] == ["completion_changed"]
    from app.services.arc_progress_reader import require_current_progress
    with pytest.raises(ArcProgressError): require_current_progress(view, completed=True)
    await submit(book, volume, "finish", closing_note="采用新安排后，作者仍决定结束")
    view = await load_arc_progress(book.db, book.pid, volume.id)
    require_current_progress(view, completed=True)
    assert view.progress["closed_early"] is (close == "early")
