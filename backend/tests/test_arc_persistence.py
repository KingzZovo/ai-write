"""Complete arc plans survive real SQLite writes, edits and source selection."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, update

from app.api import arc as api
from app.models.project import Chapter, Outline, Project, Volume, WorldRule
from app.services.arc_loop import ArcState, parse_arc_state, serialize_arc_state
from app.services.outline_authority import TEXT_REVISION, outline_for_generation, record_text_edit
from app.services.outline_order import REF, UNAVAILABLE
from app.api.arc_progress import write_arc_progress
from app.services.arc_progress_reader import load_arc_progress
from tests.support.arc_outline import arc_outline
from tests.support.arc_progress import acknowledge, chapter_ids, confirmation_request, save_manuscript, submit
from tests.test_chapter_order_maintenance import book, move, order, saved


async def create_arc(r, *, index=3, target=4):
    body = api.StartArcBody(idea="修书", core_setup="宋岚修复旧书再归还", opening_scene="检查封面",
                            target_chapters=target)
    plan = arc_outline(target)
    plan["author_material_extension"] = {"原落款": "只在原定后续揭晓"}
    plan["chapter_summaries"][0]["entity_transfers"] = [
        {"物件": "旧书", "原处": "宋岚手中", "新处": "工作台", "数量": 1}]
    await api._save_generated_arc(r.db, str(r.pid), body, index, plan, target)
    volume = r.session.scalar(select(Volume).where(Volume.project_id == r.pid, Volume.volume_idx == index))
    outline = next(row for row in r.session.scalars(select(Outline).where(Outline.project_id == r.pid,
        Outline.level == "volume")) if row.content_json.get("volume_id") == str(volume.id))
    return body, volume, outline, plan


def detached(outline):
    return SimpleNamespace(id=outline.id, project_id=outline.project_id, parent_id=outline.parent_id,
        version=outline.version, is_confirmed=outline.is_confirmed, content_json=deepcopy(outline.content_json))


async def complete(r, outline):
    volume = r.session.get(Volume, UUID(outline.content_json["volume_id"]))
    for cid in chapter_ids(r, volume):
        save_manuscript(r, cid, "林禾把书带回家，约好下月再见。")
        await acknowledge(r, volume, cid, author_note="林禾已把书带回家，约好下月再见。")


@pytest.mark.asyncio
async def test_complete_plan_and_all_chapter_drafts_share_stable_identity(book):
    before = saved(book)
    body, volume, outline, original = await create_arc(book, target=11)
    queries = list(book.queries)
    chapters = list(book.session.scalars(select(Chapter).where(Chapter.volume_id == volume.id).order_by(Chapter.chapter_idx)))
    assert [c.chapter_idx for c in chapters] == list(range(1, 12))
    assert all(c.status == "draft" and c.content_text == "" and c.word_count == 0 and c.summary is None for c in chapters)
    assert outline.is_confirmed == 1
    assert outline.content_json["volume_id"] == str(volume.id)
    assert [entry[REF] for entry in outline.content_json["chapter_summaries"]] == [str(c.id) for c in chapters]
    assert not any(entry.get(UNAVAILABLE) for entry in outline.content_json["chapter_summaries"])
    projected = outline_for_generation(outline.content_json)
    assert projected["chapter_summaries"] == original["chapter_summaries"]
    assert projected["author_material_extension"] == original["author_material_extension"]
    assert outline.content_json["_arc"]["input_material"] == {
        "idea": body.idea, "background": body.background, "core_setup": body.core_setup, "opening_scene": body.opening_scene}
    assert all(outline_for_generation(c.outline_json)["summary"] == original["chapter_summaries"][i]["summary"]
               for i, c in enumerate(chapters))
    positions = order(book)
    assert positions == book.ids[:3] + [c.id for c in chapters] + book.ids[3:]
    after = saved(book)
    assert all(after[cid]["content_text"] == row["content_text"] for cid, row in before.items())
    assert not any("chapters.content_text" in str(query) for query in queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["short", "target", "legacy", "bad_available"])
async def test_incomplete_planning_never_materializes_any_partial_volume(book, change):
    body = api.StartArcBody(idea="修书", target_chapters=4)
    plan = arc_outline(4)
    if change == "short": plan["chapter_summaries"].pop()
    elif change == "legacy": plan = {"title": "旧式半截计划", "available": True, "beats": []}
    elif change == "bad_available": plan["available"] = "false"
    before = saved(book)
    with pytest.raises(HTTPException) as exc:
        await api._save_generated_arc(book.db, str(book.pid), body, 3, plan, 5 if change == "target" else 4)
    assert exc.value.status_code in (409, 502)
    assert saved(book) == before
    assert book.session.scalar(select(Volume.id).where(Volume.project_id == book.pid, Volume.volume_idx == 3)) is None
    assert book.session.scalar(select(Outline.id).where(Outline.project_id == book.pid, Outline.level == "volume")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_ack", [False, True])
async def test_save_failure_never_reports_success_and_keeps_a_complete_transaction(book, lose_ack):
    book.lose_ack, book.fail_commit = lose_ack, not lose_ack
    with pytest.raises(HTTPException) as exc:
        await create_arc(book)
    assert exc.value.status_code == 503
    volumes = list(book.session.scalars(select(Volume).where(Volume.project_id == book.pid, Volume.volume_idx == 3)))
    if not lose_ack:
        assert volumes == []
        assert order(book) == book.ids
        assert book.session.scalar(select(Outline.id).where(Outline.project_id == book.pid, Outline.level == "volume")) is None
    else:
        assert len(volumes) == 1
        assert len(list(book.session.scalars(select(Chapter).where(Chapter.volume_id == volumes[0].id)))) == 4
        outline = book.session.scalar(select(Outline).where(Outline.project_id == book.pid, Outline.level == "volume"))
        assert len(outline_for_generation(outline.content_json)["chapter_summaries"]) == 4


@pytest.mark.asyncio
async def test_progress_updates_preserve_author_text_and_whole_plan_after_reload(book):
    _, volume, outline, _ = await create_arc(book)
    original = deepcopy(outline.content_json)
    original["_arc"]["author_extension"] = {"提醒": "保留亲属关系"}
    revised = record_text_edit(original, {**original, "raw_text": "作者新稿：不转交给第三人，二人只商量归还日期。"})
    outline.content_json = revised
    book.session.commit()
    cid = chapter_ids(book, volume)[0]
    save_manuscript(book, cid, "本章谈好日期。")
    result = await acknowledge(book, volume, cid, author_note="本章谈好日期。")
    assert len(result["progress"]["acknowledged"]) == 1
    book.session.expire_all()
    assert {k: v for k, v in outline.content_json.items() if k != "_arc"} == {k: v for k, v in revised.items() if k != "_arc"}
    assert outline.content_json["_arc"]["author_extension"] == original["_arc"]["author_extension"]
    assert outline.content_json[TEXT_REVISION] == revised[TEXT_REVISION]
    brief = await api.chapter_brief(str(book.pid), book.db)
    assert revised["raw_text"] in brief["brief"]
    assert original["chapter_summaries"][1]["summary"] not in brief["brief"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "version", "confirmation", "new_selected", "move", "delete"])
async def test_progress_write_rechecks_the_selected_arc_before_mutating(book, change):
    _, volume, outline, _ = await create_arc(book)
    vid = volume.id
    cid = chapter_ids(book, volume)[0]
    save_manuscript(book, cid)
    request = await confirmation_request(book, volume, cid)
    if change == "content":
        outline.content_json = {**outline.content_json, "author_note": "刚修改的材料"}
    elif change == "version": outline.version += 1
    elif change == "confirmation": outline.is_confirmed = 0
    elif change == "new_selected":
        book.session.add(Outline(project_id=book.pid, level="volume", is_confirmed=1,
            version=outline.version + 1, content_json=deepcopy(outline.content_json)))
    elif change == "move": volume.volume_idx = 4
    elif change == "delete": book.session.delete(outline)
    book.session.commit()
    with pytest.raises(HTTPException) as exc:
        await write_arc_progress(book.pid, vid, request, book.db)
    assert exc.value.status_code in (404, 409)
    survivors = list(book.session.scalars(select(Outline).where(Outline.project_id == book.pid, Outline.level == "volume")))
    assert all(o.content_json["_arc"]["chapters_written"] == 0 for o in survivors)
    if change == "content": assert survivors[0].content_json["author_note"] == "刚修改的材料"


@pytest.mark.asyncio
async def test_current_arc_uses_latest_confirmed_outline_for_the_real_volume(book):
    _, volume, old, _ = await create_arc(book)
    newer_content = deepcopy(old.content_json)
    newer_content["_arc"]["title"] = "作者选定的版本"
    chosen = Outline(project_id=book.pid, level="volume", is_confirmed=1, version=2, content_json=newer_content)
    unconfirmed = deepcopy(newer_content)
    unconfirmed["_arc"]["title"] = "尚未确认的后稿"
    book.session.add_all([chosen, Outline(project_id=book.pid, level="volume", is_confirmed=0, version=99, content_json=unconfirmed)])
    book.session.commit()
    index, selected, state = await api._load_current_arc(book.db, str(book.pid))
    assert index == volume.volume_idx and selected.id == chosen.id
    assert state.title == "作者选定的版本"


@pytest.mark.asyncio
async def test_replacement_volume_outline_cannot_resurrect_an_older_arc_version(book):
    _, _, old, _ = await create_arc(book)
    revised = deepcopy(old.content_json)
    revised.pop("_arc")
    book.session.add(Outline(project_id=book.pid, level="volume", is_confirmed=1, version=2, content_json=revised))
    book.session.commit()
    assert await api._load_current_arc(book.db, str(book.pid)) == (None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["orphan", "foreign", "unavailable", "wrong_label", "progress", "target", "status"])
async def test_invalid_current_source_is_explicitly_unavailable_without_guessing(book, change):
    _, _, outline, _ = await create_arc(book)
    raw = deepcopy(outline.content_json)
    if change == "orphan": raw["volume_id"] = str(uuid4())
    elif change == "foreign":
        raw["volume_id"] = str(book.session.scalar(select(Volume.id).where(Volume.project_id == book.foreign)))
    elif change == "unavailable": raw[UNAVAILABLE] = True
    elif change == "wrong_label": raw["volume_idx"] = 4
    elif change == "progress": raw["_arc"]["chapters_written"] = True
    elif change == "target": raw["_arc"]["target_chapters"] = "20"
    elif change == "status": raw["_arc"]["status"] = "unknown"
    outline.content_json = raw
    book.session.commit()
    with pytest.raises(HTTPException) as exc:
        await api._load_current_arc(book.db, str(book.pid))
    assert exc.value.status_code == 409
    assert outline.content_json == raw


@pytest.mark.asyncio
async def test_next_arc_source_preserves_authoritative_previous_material_in_full(book, monkeypatch):
    from tests.support.world_rules import install_rule_graph
    from tests.support.character_graph import CharacterGraph
    install_rule_graph(monkeypatch, book.pid)
    graph = CharacterGraph(book.pid)
    async def character_driver(): yield graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', character_driver)
    _, volume, outline, _ = await create_arc(book)
    await complete(book, outline)
    raw = "后续才揭晓署名，不得在下一弧提前兑现。" * 700
    outline.content_json = record_text_edit(outline.content_json, {**outline.content_json, "raw_text": raw})
    book.session.commit()
    await submit(book, volume, "reconcile")
    await submit(book, volume, "finish", closing_note="已核对新的弧安排，仍在这里结束。")
    previous_progress = (await load_arc_progress(book.db, book.pid, volume.id)).projected_arc().running_outline
    from app.services.arc_planning_sources import read_planning_source
    result = await read_planning_source(book.db, book.pid)
    assert result["target_volume_idx"] == 6  # Append after the actual last volume, including ordinary volumes.
    source = result["previous"]["material"]
    assert source["volume_idx"] == 3 and source["reported_progress"] == previous_progress
    assert source["outline"]["raw_text"] == raw
    assert "chapter_summaries" not in source["outline"]


@pytest.mark.asyncio
async def test_previous_arc_changes_during_planning_cannot_save_a_stale_next_arc(book, monkeypatch):
    _, volume, outline, _ = await create_arc(book)
    await complete(book, outline)
    from app.services.arc_sources import arc_source_snapshot
    expected = arc_source_snapshot(volume.volume_idx, outline)
    progress = (await load_arc_progress(book.db, book.pid, volume.id)).source
    oid = outline.id
    async def model(**kwargs):
        changed = deepcopy(outline.content_json)
        changed["author_note"] = "作者刚改变了还书经过"
        book.session.execute(update(Outline).where(Outline.id == oid).values(content_json=changed)
            .execution_options(synchronize_session=False))
        book.session.commit()
        return arc_outline(4)
    plan = await model()
    with pytest.raises(HTTPException) as exc:
        await api._save_generated_arc(book.db, str(book.pid), api.StartArcBody(idea="去看林禾", target_chapters=4),
                                     6, plan, 4, expected_previous=expected, expected_progress=progress)
    assert exc.value.status_code == 409
    assert book.session.scalar(select(Volume.id).where(Volume.project_id == book.pid, Volume.volume_idx == 6)) is None
    book.session.expire_all()
    assert outline.content_json["author_note"] == "作者刚改变了还书经过"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["brief", "progress"])
async def test_reordered_chapters_cannot_reuse_the_old_reported_progress(book, entrypoint):
    _, volume, outline, _ = await create_arc(book)
    ids = list(book.session.scalars(select(Chapter.id).where(Chapter.volume_id == volume.id).order_by(Chapter.chapter_idx)))
    save_manuscript(book, ids[0])
    await acknowledge(book, volume, ids[0], author_note="已处理原第一章。")
    url, payload = move(book, ids[0], ids[1])
    async with book.client() as client:
        moved = await client.post(url, json=payload)
    assert moved.status_code == 200, moved.text
    with pytest.raises(HTTPException) as exc:
        if entrypoint == "brief": await api.chapter_brief(str(book.pid), book.db)
        else:
            save_manuscript(book, ids[1])
            await acknowledge(book, volume, ids[1], author_note="不能借错章。")
    assert exc.value.status_code == 409
    book.session.expire_all()
    assert (await load_arc_progress(book.db, book.pid, volume.id)).public()["confirmed_count"] == 1
    assert outline.content_json["_arc"]["chapters_written"] == 0
    assert "不能借错章" not in outline.content_json["_arc"]["running_outline"]


@pytest.mark.asyncio
async def test_reordering_before_first_report_uses_the_author_chosen_order(book):
    _, volume, outline, _ = await create_arc(book)
    ids = list(book.session.scalars(select(Chapter.id).where(Chapter.volume_id == volume.id).order_by(Chapter.chapter_idx)))
    url, payload = move(book, ids[0], ids[1])
    async with book.client() as client:
        assert (await client.post(url, json=payload)).status_code == 200
    first = await api.chapter_brief(str(book.pid), book.db)
    assert arc_outline(4)["chapter_summaries"][1]["summary"] in first["brief"]
    save_manuscript(book, ids[1])
    result = await acknowledge(book, volume, ids[1], author_note="先写作者移到开头的章。")
    assert result["progress"]["chapter_order"] == [str(ids[1]), str(ids[0]), *map(str, ids[2:])]
    second = await api.chapter_brief(str(book.pid), book.db)
    assert arc_outline(4)["chapter_summaries"][0]["summary"] in second["brief"]


@pytest.mark.asyncio
async def test_manually_completed_arc_cannot_be_reopened_by_an_extra_progress_report(book):
    _, volume, outline, _ = await create_arc(book)
    outline.content_json = {**outline.content_json, "_arc": {**outline.content_json["_arc"], "status": "completed"}}
    book.session.commit()
    cid = chapter_ids(book, volume)[0]
    save_manuscript(book, cid)
    with pytest.raises(HTTPException) as exc:
        await acknowledge(book, volume, cid, author_note="不能重开。")
    assert exc.value.status_code == 409
    assert outline.content_json["_arc"]["chapters_written"] == 0
    assert outline.content_json["_arc"]["status"] == "completed"


@pytest.mark.asyncio
async def test_current_arc_lookup_in_large_book_reads_only_the_selected_full_outline(book):
    prose = "旧章的正文保持不变。" * 520
    count = 996
    assert len(prose) * count > 5_000_000
    book.session.execute(update(Project).where(Project.id == book.pid).values(target_word_count=5_000_000))
    volumes = [uuid4() for _ in range(125)]
    book.session.execute(insert(Volume), [dict(id=vid, project_id=book.pid, volume_idx=10+i, title=f"旧弧{i}")
                                          for i, vid in enumerate(volumes)])
    book.session.execute(insert(Chapter), [dict(id=uuid4(), volume_id=volumes[i//8], chapter_idx=i%8+1,
        global_idx=i+6, title=f"旧章{i}", content_text=prose, status="draft") for i in range(count)])
    old_content = "以前的整弧长计划，不属于当前弧材料。" * 500
    for i, vid in enumerate(volumes):
        state = ArcState(title=f"旧弧{i}", core_setup="旧约定", opening_scene="", target_chapters=8,
                         status="completed", chapters_written=8)
        book.session.add(Outline(project_id=book.pid, level="volume", is_confirmed=1,
            content_json=serialize_arc_state(state, volume_idx=10+i, base={"volume_id": str(vid), "raw_text": old_content})))
    book.session.commit()
    _, volume, current, _ = await create_arc(book, index=200)
    current_id = current.id
    book.session.expire_all()
    book.queries.clear()
    index, selected, _ = await api._load_current_arc(book.db, str(book.pid))
    assert index == 200 and selected.id == current_id
    full_reads = [q for q in book.queries if any(
        getattr(column, "table", None) is Outline.__table__ and column.key == "content_json"
        for column in getattr(q, "selected_columns", []))]
    assert len(full_reads) == 1
    assert current_id in full_reads[0].compile().params.values()
    assert not any("chapters.content_text" in str(q) for q in book.queries)
    assert book.session.scalar(select(Chapter.content_text).where(Chapter.volume_id == volumes[0]).limit(1)) == prose
