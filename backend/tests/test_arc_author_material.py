"""Actual writer inputs retain the author's words, not just a model's plan."""
from copy import deepcopy

import pytest

from app.api import arc as arc_api
from app.services.arc_progress_contract import ArcProgressError
from app.services.arc_progress_material import load_arc_writing_material
from app.services.context_budget import ContextBudgetExceeded
from app.services.context_pack import ContextPack
from app.services.context_pack import RequiredContextUnavailable
from app.services.outline_authority import outline_for_generation, record_text_edit
from tests.support.arc_progress import chapter_ids, submit
from tests.test_arc_persistence import create_arc
from tests.test_chapter_order_maintenance import book, move


def author_input(outline, **changes):
    content = deepcopy(outline.content_json)
    material = {"idea": "姐弟在屋内商量还款，不临时引入第三人解围。",
        "background": "欠款三千元；姐姐拿出两千元，余下一千元。" * 80,
        "core_setup": "缝纫收入不等于固定发薪，人物不知道的事不可突然知晓。",
        "opening_scene": "只给原开场：姐姐把装有两千元的信封推到桌子中间。",
        "作者补充": {"详略": ["写清商量还款日期的过程。", "允许正常合作和自然收束。"]}}
    material.update(changes)
    content["_arc"]["input_material"] = material
    outline.content_json = content
    return material


@pytest.mark.asyncio
async def test_original_author_words_reach_actual_writer_in_full_and_are_not_optional(book):
    _, volume, outline, _ = await create_arc(book)
    material = author_input(outline)
    book.session.commit()
    original = deepcopy(outline.content_json)
    ids = chapter_ids(book, volume)
    first = await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)
    for key in ("idea", "background", "core_setup", "opening_scene"):
        assert material[key] in first
    for value in material["作者补充"]["详略"]:
        assert value in first
    later = await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)
    assert material["background"] in later and material["core_setup"] in later
    assert material["opening_scene"] not in later
    pack = ContextPack(volume_outline=outline_for_generation(outline.content_json), arc_author_guidance=first)
    prompt = pack.to_system_prompt(token_budget=9000)
    assert material["background"] in prompt and material["opening_scene"] in prompt
    assert any(row["source"] == "arc_author_guidance" and row["required"] for row in pack.budget_trace)
    with pytest.raises(ContextBudgetExceeded):
        pack.to_system_prompt(token_budget=100)
    assert outline.content_json == original


@pytest.mark.asyncio
async def test_reordering_does_not_assign_original_opening_to_a_different_chapter(book):
    _, volume, outline, _ = await create_arc(book)
    material = author_input(outline)
    book.session.commit()
    ids = chapter_ids(book, volume)
    url, payload = move(book, ids[0], ids[1])
    async with book.client() as client:
        assert (await client.post(url, json=payload)).status_code == 200
    await submit(book, volume, "reconcile")
    first_now = await load_arc_writing_material(book.db, book.pid, volume.id, ids[1], outline)
    original_first = await load_arc_writing_material(book.db, book.pid, volume.id, ids[0], outline)
    assert material["opening_scene"] not in first_now
    assert material["opening_scene"] in original_first
    assert material["opening_scene"] not in (await arc_api.chapter_brief(str(book.pid), book.db))["brief"]


@pytest.mark.asyncio
async def test_author_text_revision_replaces_old_inputs_and_is_rendered_exactly_once(book):
    from app.services.arc_progress_material import load_arc_writing_context

    _, volume, outline, _ = await create_arc(book)
    material = author_input(outline)
    revised = "作者后来改稿：不转交信封，二人只商量新的还款日期。" * 100
    outline.content_json = record_text_edit(outline.content_json, {**outline.content_json, "raw_text": revised})
    book.session.commit()
    saved = deepcopy(outline.content_json)
    context = await load_arc_writing_context(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)
    assert revised in context.text and material["background"] not in context.text
    pack = ContextPack(volume_outline=outline_for_generation(outline.content_json), arc_author_guidance=context.text,
                       arc_replaces_volume_outline=context.replaces_volume_outline)
    assert pack.to_system_prompt(token_budget=9000).count(revised) == 1
    assert not any(row["source"] == "volume_outline" for row in pack.budget_trace)
    assert (await arc_api.chapter_brief(str(book.pid), book.db))["brief"].count(revised) == 1
    with pytest.raises(ContextBudgetExceeded): pack.to_system_prompt(token_budget=100)
    assert outline.content_json == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["damaged input", {"idea": [], "opening_scene": "开场"},
                                     {"background": False}, {"opening_scene": {"旧章": "无法确认"}}])
async def test_malformed_author_material_stops_writing_instead_of_being_silently_omitted(book, invalid):
    _, volume, outline, _ = await create_arc(book)
    content = deepcopy(outline.content_json)
    content["_arc"]["input_material"] = invalid
    outline.content_json = content
    book.session.commit()
    with pytest.raises(ArcProgressError, match="作者材料"):
        await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)


@pytest.mark.asyncio
async def test_unbound_opening_is_not_assigned_using_the_current_chapter_number(book):
    _, volume, outline, _ = await create_arc(book)
    author_input(outline)
    content = deepcopy(outline.content_json)
    del content["_arc"]["chapter_order"]
    outline.content_json = content
    book.session.commit()
    with pytest.raises(ArcProgressError, match="开场"):
        await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)


@pytest.mark.asyncio
async def test_author_text_revision_does_not_revive_damaged_older_material(book):
    _, volume, outline, _ = await create_arc(book)
    content = deepcopy(outline.content_json)
    content["_arc"]["input_material"] = "旧结构已坏，原件须留存"
    current = "作者现稿：从商量新的还款日期写起，旧设定已改。"
    outline.content_json = record_text_edit(content, {**content, "raw_text": current})
    book.session.commit()
    result = await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)
    assert current in result and content["_arc"]["input_material"] not in result


@pytest.mark.asyncio
async def test_older_bound_arc_keeps_its_original_setup_without_inventing_missing_inputs(book):
    body, volume, outline, _ = await create_arc(book)
    content = deepcopy(outline.content_json)
    del content["_arc"]["input_material"]
    outline.content_json = content
    book.session.commit()
    result = await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)
    assert body.core_setup in result and body.opening_scene in result
    assert "没有提供的背景" not in result


@pytest.mark.asyncio
async def test_empty_author_revision_requires_review_instead_of_restoring_original_setup(book):
    _, volume, outline, _ = await create_arc(book)
    author_input(outline)
    outline.content_json = record_text_edit(outline.content_json, {**outline.content_json, "raw_text": ""})
    book.session.commit()
    with pytest.raises(ArcProgressError, match="当前文字大纲为空"):
        await load_arc_writing_material(book.db, book.pid, volume.id, chapter_ids(book, volume)[0], outline)


def test_missing_required_author_text_cannot_silently_disable_the_volume_material():
    pack = ContextPack(volume_outline={"raw_text": "作者已改写本卷。"}, arc_replaces_volume_outline=True)
    with pytest.raises(RequiredContextUnavailable):
        pack.to_system_prompt(token_budget=9000)
