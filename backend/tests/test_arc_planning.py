"""Arc planning must deliver every requested chapter through the real Registry."""
import asyncio
import copy
import json
import math
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api import arc as arc_api
from app.services import arc_loop, prompt_registry
from tests.support.arc_outline import arc_meta, arc_chapter, arc_outline


def response(data, finish="stop"):
    return SimpleNamespace(text=data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                           finish_reason=finish)


def install_model(monkeypatch, target, *, first_bad=None, fail_batch=None, finish="stop"):
    history = []
    failed_once = False

    async def generate(task, user, db, background="", **kw):
        nonlocal failed_once
        history.append((task, user, background, kw))
        if task == "arc_outline_meta":
            return response(arc_meta(target))
        if task == "arc_outline_batch":
            match = re.search(r"start=(\d+), end=(\d+)", user)
            assert match
            start, end = map(int, match.groups())
            payload = {"chapter_summaries": [arc_chapter(i) for i in range(start, end + 1)]}
            if fail_batch == start:
                return response({"chapter_summaries": []}, finish)
            if first_bad is not None and not failed_once:
                failed_once = True
                return response(first_bad(copy.deepcopy(payload)), finish)
            return response(payload)
        # The old planner accepted two one-line beats for any target.
        return response({"title": "半截弧纲", "beats": [{"chapter": 1, "beat": "还书"},
                                                   {"chapter": 2, "beat": "约好日期"}]})

    call = AsyncMock(side_effect=generate)
    monkeypatch.setattr(prompt_registry, "run_text_prompt", call)
    return call, history


async def generate_arc(target=4, **kwargs):
    return await arc_loop.generate_arc_outline(
        idea="修补旧书", background="宋岚借了林禾的书，封面松脱。", core_setup="修好后归还",
        opening_scene="宋岚在店里检查封面", target_chapters=target, db=None,
        project_id="p", **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [4, 11, 40])
async def test_arc_delivers_exact_complete_chapter_sequence(monkeypatch, target):
    model, history = install_model(monkeypatch, target)
    result = await generate_arc(target)
    assert result["available"] is True and result["chapter_count"] == target
    assert [c["chapter_idx"] for c in result["chapter_summaries"]] == list(range(1, target + 1))
    assert result["chapter_summaries"][0]["character_choices"] == arc_chapter(1)["character_choices"]
    assert model.await_count == 1 + math.ceil(target / 5)
    assert {h[0] for h in history} == {"arc_outline_meta", "arc_outline_batch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["short", "extra", "duplicate", "wrong_type", "summary", "events", "handoff"])
async def test_bad_batch_is_repaired_with_original_input_and_full_output(monkeypatch, bad_kind):
    rejected = []

    def bad(payload):
        items = payload["chapter_summaries"]
        if bad_kind == "short": items.pop()
        elif bad_kind == "extra": items.append(arc_chapter(5))
        elif bad_kind == "duplicate": items[1]["chapter_idx"] = 1
        elif bad_kind == "wrong_type": items[0]["chapter_idx"] = True
        elif bad_kind == "summary": items[0]["summary"] = {"过程": "不能强转成正文"}
        elif bad_kind == "events": items[0]["key_events"] = [True]
        elif bad_kind == "handoff": del items[0]["handoff_to_next"]
        rejected.append(copy.deepcopy(payload))
        return payload

    model, history = install_model(monkeypatch, 4, first_bad=bad)
    result = await generate_arc()
    assert result["available"] is True and len(result["chapter_summaries"]) == 4
    assert model.await_count == 3
    repair = history[-1][1]
    assert json.dumps(rejected[0], ensure_ascii=False) in repair
    assert "宋岚借了林禾的书，封面松脱。" in repair
    assert "不是已发生事实" in repair


@pytest.mark.asyncio
async def test_later_batches_keep_full_early_plan_and_previous_arc(monkeypatch):
    model, history = install_model(monkeypatch, 11)
    previous = {"reported_progress": "原弧由作者记录的进展。" * 500,
                "outline": {"raw_text": "后续才揭晓书页中的署名，不在当前弧提前兑现。"}}
    await generate_arc(11, previous_arc=previous)
    last = history[-1][1]
    assert previous["reported_progress"] in last
    assert previous["outline"]["raw_text"] in last
    assert json.dumps(arc_chapter(1), ensure_ascii=False) in last
    assert "宋岚借了林禾的书，封面松脱。" in last
    assert model.await_count == 4


@pytest.mark.asyncio
async def test_exhausted_batch_stops_remaining_calls_and_cannot_report_success(monkeypatch):
    model, history = install_model(monkeypatch, 11, fail_batch=6)
    result = await generate_arc(11)
    assert result["available"] is False
    assert model.await_count == 4  # meta, 1-5, 6-10, repair 6-10
    assert not any("start=11" in h[1] for h in history)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [0, -1, True, "20", 4.5, 41])
async def test_invalid_target_is_not_replaced_by_a_different_request(monkeypatch, target):
    model, _ = install_model(monkeypatch, 4)
    result = await generate_arc(target)
    assert result["available"] is False and model.await_count == 0


@pytest.mark.parametrize("target", [0, -1, True, "20", 4.5, 41])
def test_arc_request_rejects_invalid_original_chapter_count(target):
    with pytest.raises(ValidationError):
        arc_api.StartArcBody(idea="修书", target_chapters=target)


@pytest.mark.asyncio
async def test_arc_cancellation_does_not_start_repair_or_next_batch(monkeypatch):
    call = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(prompt_registry, "run_text_prompt", call)
    with pytest.raises(asyncio.CancelledError):
        await generate_arc()
    assert call.await_count == 1


def test_full_current_chapter_plan_reaches_brief_without_future_events():
    outline = arc_outline(4)
    outline["chapter_summaries"][0]["key_events"] = ["第一章才发生的独立过程"]
    outline["chapter_summaries"][3]["key_events"] = ["第四章尚未兑现的独立过程"]
    state = arc_loop.ArcState(title="修好以后", core_setup="旧书", opening_scene="检查",
                              target_chapters=4, chapters_written=1, next_direction="本章先听林禾说完。")
    brief = arc_loop.build_next_chapter_brief(state, arc_outline=outline)
    assert json.dumps(outline["chapter_summaries"][1], ensure_ascii=False) in brief
    assert "本章先听林禾说完。" in brief
    assert "第四章尚未兑现的独立过程" not in brief
    assert "第一章才发生的独立过程" not in brief


def test_author_text_revision_overrides_old_arc_structure():
    from app.services.outline_authority import record_text_edit
    old = arc_outline(4)
    old["chapter_summaries"][0]["summary"] = "旧版把书交给第三人。"
    current = record_text_edit(old, {**old, "raw_text": "作者改稿：旧书始终由林禾保管，本章二人只谈日期。"})
    state = arc_loop.ArcState(title="修书", core_setup="", opening_scene="", target_chapters=4)
    brief = arc_loop.build_next_chapter_brief(state, arc_outline=current)
    assert current["raw_text"] in brief and "旧版把书交给第三人。" not in brief


@pytest.mark.parametrize("beats", [
    [], [{"chapter": True, "beat": "错误编号"}], [{"chapter": "1", "beat": "错误编号"}],
    [{"chapter": 1, "beat": True}], [{"chapter": 1, "beat": "甲"}, {"chapter": 1, "beat": "乙"}],
])
def test_legacy_brief_cannot_guess_missing_or_ambiguous_chapter(beats):
    state = arc_loop.ArcState(title="旧弧", core_setup="", opening_scene="", target_chapters=4)
    with pytest.raises(ValueError):
        arc_loop.build_next_chapter_brief(state, arc_beats=beats)


def test_arc_state_serialization_keeps_full_plan_and_author_metadata():
    from app.services.outline_authority import record_text_edit, TEXT_REVISION
    old = {**arc_outline(4), "volume_idx": 2, "volume_id": "original-volume",
           "author_notes": ["保留母亲的落款"], "_arc": {"author_extension": {"note": "仍留给作者"}}}
    old = arc_loop.serialize_arc_state(arc_loop.ArcState(title="修好以后", core_setup="旧书", opening_scene="检查",
        target_chapters=4), volume_idx=2, base=old)
    old = record_text_edit(old, {**old, "raw_text": "作者完整改稿"})
    state = arc_loop.ArcState(title="修好以后", core_setup="旧书", opening_scene="检查",
                              target_chapters=4, chapters_written=1, status="awaiting_direction")
    current = arc_loop.serialize_arc_state(state, volume_idx=2, base=old)
    assert {k: v for k, v in current.items() if k != "_arc"} == {k: v for k, v in old.items() if k != "_arc"}
    assert current["_arc"]["chapters_written"] == 1
    assert current["_arc"]["author_extension"] == old["_arc"]["author_extension"]
    assert current[TEXT_REVISION] == old[TEXT_REVISION]
    assert old["_arc"]["chapters_written"] == 0
