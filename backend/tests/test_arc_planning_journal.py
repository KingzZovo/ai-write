"""Interrupted planning keeps accepted chapters, complete inputs and spent calls."""
import copy
import importlib
import json
from uuid import uuid4

import pytest

from tests.support.arc_outline import arc_meta, arc_chapter


STAMP = "2026-10-09T12:00:00+00:00"


def journal_api():
    return importlib.import_module("app.services.arc_planning_journal")


def material(target=11):
    return dict(idea="渡口还钱", background="欠三千，只带来两千。" * 300,
                core_setup="先商量余款，不临时制造收入", opening_scene="信封在碗下", target_chapters=target)


def route(task):
    return dict(version=1, task_type=task, prompt_id=str(uuid4()), endpoint_id=str(uuid4()),
                model="configured-model", temperature=0.7, max_tokens=8192,
                system_prompt="依据作者完整材料写中文剧情规划。", mode="structured", preferred_tier=None)


def accept(state, api):
    call = api.pending_call(state)
    output = arc_meta(state["target_chapters"]) if call["key"] == "meta" else {
        "chapter_summaries": [arc_chapter(i) for i in range(call["start"], call["end"] + 1)]}
    return api.finish_attempt(state, call["key"], call["number"], output=output,
                              raw_text=json.dumps(output, ensure_ascii=False), error="", now=STAMP)


def test_restart_skips_complete_batches_and_keeps_full_source():
    api = journal_api(); source = material()
    state = api.new_journal(source, {"reported_progress": "弟弟明日交租，是否缓交未知。"})
    for _ in range(2):
        spec = api.next_stage(state)
        state = api.reserve_attempt(state, route(spec["task"]), now=STAMP)
        state = accept(state, api)
    prior = copy.deepcopy(state)
    spec = api.next_stage(state)
    assert spec["key"] == "chapters_6_10"
    state = api.reserve_attempt(state, route(spec["task"]), now=STAMP)
    frozen_route = copy.deepcopy(api.pending_call(state)["route"])
    # Process died after reservation. Reload the actual serialized checkpoint.
    state = json.loads(json.dumps(state, ensure_ascii=False))
    state = api.reserve_attempt(state, None, now=STAMP)
    call = api.pending_call(state)
    assert call["number"] == 2 and call["route"] == frozen_route
    assert source["background"] in call["input"]
    assert "弟弟明日交租，是否缓交未知。" in call["input"]
    assert json.dumps(arc_chapter(1), ensure_ascii=False) in call["input"]
    assert state["stages"][:2] == prior["stages"]
    assert state["stages"][-1]["attempts"][0]["state"] == "interrupted"


def test_two_interrupted_calls_cannot_buy_a_fresh_budget():
    api = journal_api(); state = api.new_journal(material(4), None)
    state = api.reserve_attempt(state, route("arc_outline_meta"), now=STAMP)
    state = api.reserve_attempt(state, None, now=STAMP)
    with pytest.raises(api.ArcPlanningBudgetExhausted):
        api.reserve_attempt(json.loads(json.dumps(state)), None, now=STAMP)


def test_failed_output_is_preserved_whole_in_the_repair():
    api = journal_api(); state = api.new_journal(material(4), None)
    state = api.reserve_attempt(state, route("arc_outline_meta"), now=STAMP)
    raw = '原版完整输出。' * 2000
    state = api.finish_attempt(state, "meta", 1, output=None, raw_text=raw, error="格式不完整", now=STAMP)
    state = api.reserve_attempt(state, None, now=STAMP)
    assert raw in api.pending_call(state)["input"]
    assert api.pending_call(state)["temperature"] == 0.0


@pytest.mark.parametrize("change", ["target", "source", "input", "attempt", "output", "stage", "empty"])
def test_damaged_or_rebased_checkpoint_cannot_resume(change):
    api = journal_api(); source = material(4)
    state = api.new_journal(source, None)
    state = api.reserve_attempt(state, route("arc_outline_meta"), now=STAMP)
    state = accept(state, api)
    if change == "target": state["target_chapters"] = 5
    elif change == "source": state["source_prompt"] = "重新概括过的材料"
    elif change == "input": state["stages"][0]["attempts"][0]["input"] = "新输入"
    elif change == "attempt": state["stages"][0]["attempts"][0]["number"] = 0
    elif change == "output": state["stages"][0]["attempts"][0]["output"]["chapter_count"] = 3
    elif change == "stage": state["stages"][0]["key"] = "chapters_1_4"
    elif change == "empty": state = {}
    with pytest.raises((ValueError, TypeError)):
        api.validate_journal(state, source, None)


def test_complete_plan_survives_recovery_without_rewriting_any_stage():
    api = journal_api(); state = api.new_journal(material(11), None)
    while (spec := api.next_stage(state)) is not None:
        state = api.reserve_attempt(state, route(spec["task"]), now=STAMP)
        state = accept(state, api)
    restored = json.loads(json.dumps(state, ensure_ascii=False))
    plan = api.complete_outline(restored)
    assert plan["chapter_count"] == 11
    assert [c["chapter_idx"] for c in plan["chapter_summaries"]] == list(range(1, 12))
    assert api.accepted_chapters(restored) == 11
    assert api.next_stage(restored) is None


def test_late_result_cannot_overwrite_the_reserved_recovery_attempt():
    api = journal_api(); state = api.new_journal(material(4), None)
    state = api.reserve_attempt(state, route("arc_outline_meta"), now=STAMP)
    state = api.reserve_attempt(state, None, now=STAMP)
    with pytest.raises(ValueError):
        api.finish_attempt(state, "meta", 1, output=arc_meta(4), raw_text="{}", error="", now=STAMP)
