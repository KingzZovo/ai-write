"""Previous author decisions and original text survive the durable plan boundary."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.api.arc_progress import ArcProgressBody, write_arc_progress
from app.models.project import Chapter, Outline, Volume
from app.services.arc_progress_reader import load_arc_progress
from app.services.outline_authority import record_text_edit
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit, volume_count


async def finish_original(r, request_id):
    receipt = snapshot(r, request_id)["result_json"]
    vid = UUID(receipt["volume_id"])
    for index, identity in enumerate(receipt["chapter_ids"], 1):
        content = f"第{index}章：姐弟把原有账目核对清楚，信封仍由弟弟收着。"
        with r.engine.begin() as db:
            db.execute(sa.update(Chapter).where(Chapter.id == UUID(identity)).values(content_text=content))
        async with r.sessions() as db:
            view = await load_arc_progress(db, r.pid, vid)
            chapter = await db.get(Chapter, identity)
            await write_arc_progress(r.pid, vid, ArcProgressBody(request_id=str(uuid4()), expected_source=view.source,
                action="acknowledge", chapter_id=identity, expected_content_text=content, expected_title=chapter.title,
                author_note=f"作者核对过第{index}章的交款经过。"), db)
    return receipt


@pytest.mark.asyncio
async def test_actual_worker_receives_complete_current_author_text_and_formal_previous_progress(planning_book):
    r = planning_book; first = await request_body(r, target=4)
    await submit(r, first); await run(r, first["request_id"])
    receipt = await finish_original(r, first["request_id"])
    full = "余款仍是一千元，房东是否缓收还不知道，不能另添订货收入。" * 700
    with Session(r.engine) as db:
        outline = db.get(Outline, UUID(receipt["outline_id"]))
        outline.content_json = record_text_edit(outline.content_json, {**outline.content_json, "raw_text": full})
        db.commit()
    vid = UUID(receipt["volume_id"])
    for action in ("reconcile", "finish"):
        async with r.sessions() as db:
            view = await load_arc_progress(db, r.pid, vid)
            await write_arc_progress(r.pid, vid, ArcProgressBody(request_id=str(uuid4()), expected_source=view.source,
                action=action, closing_note="按当前修改后的安排结束前弧。" if action == "finish" else ""), db)
    second = await request_body(r, target=4)
    assert second["mode"] == "next"
    await submit(r, second); await run(r, second["request_id"])
    state = snapshot(r, second["request_id"])
    assert state["status"] == "completed" and state["target_volume_idx"] == 7
    previous = state["source_json"]["previous"]["material"]
    assert previous["outline"]["raw_text"] == full
    assert "chapter_summaries" not in previous["outline"]
    assert previous["outline"]["volume_id"] == receipt["volume_id"]
    assert previous["outline"]["volume_idx"] == 6
    assert previous["outline"]["chapter_count"] == 4
    assert "作者核对过第4章的交款经过。" in previous["reported_progress"]
    assert all(full in user for _, user, _ in r.calls[-2:])
    assert all(previous["reported_progress"].splitlines()[0] in user for _, user, _ in r.calls[-2:])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["outline", "manuscript"])
async def test_previous_arc_changes_during_a_real_model_phase_cannot_materialize_a_stale_plan(planning_book, change):
    r = planning_book; first = await request_body(r, target=4)
    await submit(r, first); await run(r, first["request_id"])
    receipt = await finish_original(r, first["request_id"])
    second = await request_body(r, target=4)
    await submit(r, second)
    before = deepcopy(snapshot(r, second["request_id"])["source_json"])
    async def change_source(task, user, writer, kwargs):
        # Simulate an external/legacy committed change which did not use the
        # canonical writer mutex. The source guard still must reject it.
        with Session(r.engine) as db:
            if change == "outline":
                row = db.get(Outline, UUID(receipt["outline_id"]))
                row.content_json = {**row.content_json, "author_note": "作者刚改了结尾安排。"}
            else:
                row = db.get(Chapter, UUID(receipt["chapter_ids"][0]))
                row.content_text = "作者的新稿：弟弟尚未收到信封。"
            db.commit()
    r.before_call = change_source
    await run(r, second["request_id"])
    state = snapshot(r, second["request_id"])
    assert state["status"] == "needs_review" and state["source_json"] == before
    assert volume_count(r) == 2 and len(r.calls) == 3
    with Session(r.engine) as db:
        if change == "outline": assert db.get(Outline, UUID(receipt["outline_id"])).content_json["author_note"] == "作者刚改了结尾安排。"
        else: assert db.get(Chapter, UUID(receipt["chapter_ids"][0])).content_text == "作者的新稿：弟弟尚未收到信封。"


@pytest.mark.asyncio
async def test_source_and_append_work_for_a_five_million_character_book_without_reading_all_prose(planning_book):
    r = planning_book
    prose = "渡口的水涨了，姐弟仍照原来商量的办法还钱。" * 250
    with r.engine.begin() as db:
        db.execute(sa.insert(Chapter), [dict(id=uuid4(), volume_id=r.vid, title=f"旧章{index}",
            chapter_idx=index + 2, global_idx=index + 2, content_text=prose) for index in range(1005)])
        size = db.scalar(sa.select(sa.func.sum(sa.func.length(Chapter.content_text))).where(Chapter.volume_id == r.vid))
    assert size >= 5_000_000
    r.queries.clear()
    body = await request_body(r, target=4)
    await submit(r, body); await run(r, body["request_id"])
    source_queries = list(r.queries)
    state = snapshot(r, body["request_id"])
    assert state["status"] == "completed", state["error_message"]
    assert not any('chapters.content_text' in query for query in source_queries if query.startswith('SELECT'))
    assert len(state["source_json"]["volume_order"]) == 1
    with Session(r.engine) as db:
        first = db.get(Chapter, UUID(state["result_json"]["chapter_ids"][0]))
        assert first.global_idx == 1007
        assert db.scalar(sa.select(sa.func.count()).select_from(Chapter).where(Chapter.volume_id == r.vid)) == 1006
    import json
    from pathlib import Path
    Path('/tmp/aiwrite-arc-planning-scale-20261009.json').write_text(json.dumps({
        'existing_chapters': 1006, 'existing_characters': size, 'new_chapter_drafts': 4,
        'first_new_global_idx': 1007, 'whole_book_prose_selected': False}, ensure_ascii=False, indent=2))
