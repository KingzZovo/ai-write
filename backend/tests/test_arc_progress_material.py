"""Author arc directions reach the writer context without leaking future notes."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.api import arc as api
from app.models.project import Chapter, Outline
from app.services.arc_progress_contract import ArcProgressError
from app.services.arc_progress_material import load_arc_writing_material
from app.services.arc_progress_reader import load_arc_progress
from app.services.context_budget import ContextBudgetExceeded
from app.services.context_pack import ContextPack
from tests.support.arc_progress import acknowledge, chapter_ids, save_manuscript, submit
from tests.test_arc_persistence import create_arc, complete
from tests.test_chapter_order_maintenance import book, move


@pytest.mark.asyncio
async def test_direction_binds_to_a_chapter_and_future_confirmation_never_becomes_prior_plot(book):
    _, volume, outline, _ = await create_arc(book)
    ids = chapter_ids(book, volume)
    direction = "先听林禾把借书的缘由说完，不新增阻拦或争执。" * 120
    await submit(book, volume, "direction", direction=direction)
    save_manuscript(book, ids[2])
    await acknowledge(book, volume, ids[2], author_note="这段只有第三章才发生：林禾已带书离开。")
    first = await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)
    assert direction in first and "只有第三章" not in first
    assert direction in (await api.chapter_brief(str(book.pid), book.db))["brief"]
    assert "只有第三章" not in (await api.chapter_brief(str(book.pid), book.db))["brief"]
    second = await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)
    assert direction not in second and "只有第三章" not in second
    fourth = await load_arc_writing_material(book.db, book.pid, volume.id, ids[3], outline)
    assert direction not in fourth and "只有第三章" in fourth
    pack = ContextPack(arc_author_guidance=first)
    assert direction in pack.to_system_prompt(token_budget=6000)
    assert any(row["source"] == "arc_author_guidance" and row["required"] for row in pack.budget_trace)
    with pytest.raises(ContextBudgetExceeded): pack.to_system_prompt(token_budget=100)


@pytest.mark.asyncio
async def test_reordering_does_not_move_a_saved_direction_to_a_different_chapter(book):
    _, volume, outline, _ = await create_arc(book)
    ids = chapter_ids(book, volume)
    await submit(book, volume, "direction", direction="只给原第一章的安排。")
    url, payload = move(book, ids[0], ids[1])
    async with book.client() as client:
        assert (await client.post(url, json=payload)).status_code == 200
    with pytest.raises(ArcProgressError):
        await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)
    await submit(book, volume, "reconcile")
    assert "只给原第一章" not in await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)
    assert "只给原第一章" in await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)
    assert "只给原第一章" not in (await api.chapter_brief(str(book.pid), book.db))["brief"]
    await submit(book, volume, "direction", direction="给当前第一章的另一安排。")
    assert "另一安排" in await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)


@pytest.mark.asyncio
async def test_new_arc_retains_its_opening_without_creating_an_unbound_legacy_direction(book):
    body, volume, outline, _ = await create_arc(book)
    view = (await load_arc_progress(book.db, book.pid, volume.id)).public()
    assert view["issues"] == [] and not view["legacy"]["unverified"]
    assert view["progress"]["direction"] is None
    assert outline.content_json["_arc"]["next_direction"] is None
    assert outline.content_json["_arc"]["input_material"]["opening_scene"] == body.opening_scene
    assert body.opening_scene in (await api.chapter_brief(str(book.pid), book.db))["brief"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_count", [0, 2])
async def test_legacy_direction_needs_an_explicit_author_chapter_binding(book, reported_count):
    from app.models.arc_progress import ArcProgressWrite

    _, volume, outline, _ = await create_arc(book)
    ids = chapter_ids(book, volume)
    legacy_direction = "旧方向：只在离家后的下一章寄信，原章节身份没有保存。"
    outline.content_json = {**outline.content_json, "_arc": {**outline.content_json["_arc"],
        "chapters_written": reported_count, "next_direction": legacy_direction}}
    book.session.commit()
    before = (await load_arc_progress(book.db, book.pid, volume.id)).public()
    assert before["legacy"]["unverified"]
    assert before["progress"]["direction"] is None
    assert before["progress"]["direction_chapter_id"] is None
    with pytest.raises(ArcProgressError):
        await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)

    adopted = await submit(book, volume, "reconcile")
    history = book.session.get(ArcProgressWrite, UUID(adopted["request_id"]))
    assert history.before_json["legacy"]["next_direction"] == legacy_direction
    for cid in ids:
        assert legacy_direction not in await load_arc_writing_material(book.db, book.pid, volume.id, cid, outline)
    assert legacy_direction not in (await api.chapter_brief(str(book.pid), book.db))["brief"]

    # The same words become usable only after the author explicitly assigns
    # them to the currently selected pending chapter through the new contract.
    await submit(book, volume, "direction", direction=legacy_direction)
    assert legacy_direction in await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)
    assert legacy_direction not in await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manuscript", "outline", "deleted_confirmation", "legacy"])
async def test_stale_author_progress_cannot_enter_writing_material(book, change):
    _, volume, outline, _ = await create_arc(book)
    ids = chapter_ids(book, volume)
    if change == "legacy":
        outline.content_json = {**outline.content_json, "_arc": {**outline.content_json["_arc"], "chapters_written": 1}}
    else:
        save_manuscript(book, ids[0]); await acknowledge(book, volume, ids[0])
        if change == "manuscript": save_manuscript(book, ids[0], "作者修改了第一章。")
        elif change == "outline": outline.content_json = {**outline.content_json, "author_extra": "刚改变安排"}
        else: book.session.delete(book.session.get(Chapter, ids[0]))
    book.session.commit()
    with pytest.raises(ArcProgressError):
        await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)


@pytest.mark.asyncio
async def test_non_arc_material_does_not_require_arc_history_tables_or_queries():
    for outline in (None, SimpleNamespace(content_json={"summary": "普通卷纲"}),
                    SimpleNamespace(content_json={"_arc": {"is_arc": False}})):
        assert await load_arc_writing_material(object(), "p", "v", "c", outline) == ""


@pytest.mark.asyncio
async def test_stale_selected_outline_cannot_supply_a_different_versions_author_context(book):
    _, volume, outline, _ = await create_arc(book)
    original = SimpleNamespace(id=outline.id, version=outline.version, content_json=deepcopy(outline.content_json))
    outline.content_json = {**outline.content_json, "author_extra": "新来源"}
    book.session.commit()
    with pytest.raises(ArcProgressError, match="大纲"):
        await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], original)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reopen", "retract", "manuscript"])
async def test_next_arc_save_rechecks_author_progress_after_planning(book, monkeypatch, change):
    from tests.support.arc_outline import arc_outline
    _, volume, outline, _ = await create_arc(book)
    await complete(book, outline)
    ids = chapter_ids(book, volume)
    from app.services.arc_sources import arc_source_snapshot
    from app.services.arc_progress_reader import load_arc_progress
    expected = arc_source_snapshot(volume.volume_idx, outline)
    progress = (await load_arc_progress(book.db, book.pid, volume.id)).source
    async def model(**kwargs):
        if change == "reopen": await submit(book, volume, "reopen")
        elif change == "retract": await submit(book, volume, "retract", chapter_id=str(ids[0]))
        else: save_manuscript(book, ids[0], "作者刚修改前弧原稿。")
        return arc_outline(4)
    plan = await model()
    with pytest.raises(HTTPException) as exc:
        await api._save_generated_arc(book.db, str(book.pid), api.StartArcBody(idea="下一弧", target_chapters=4),
                                     6, plan, 4, expected_previous=expected, expected_progress=progress)
    assert exc.value.status_code == 409
    assert len(list(book.session.scalars(select(Outline).where(Outline.project_id == book.pid, Outline.level == 'volume')))) == 1
