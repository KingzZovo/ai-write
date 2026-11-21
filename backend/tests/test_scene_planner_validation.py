"""Exercise the real structured planner boundary, not a mocked plan parser."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings
from app.services import prompt_registry, scene_orchestrator
from app.services.scene_orchestrator import SceneOrchestrator
from tests.test_c1_scene_orchestrator import _FakePack, _scene_contract


def scene(**overrides):
    return _scene_contract(
        **{"title": "归还", "brief": "宋岚将借来的书交还林禾，两人约定明日下午见。",
           "target_words": 1800, "hook": "", **overrides}
    )


def reply(payload, finish="stop"):
    return SimpleNamespace(
        text=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        finish_reason=finish,
    )


async def plan(*, target=1800, instruction="", pack=None):
    return await SceneOrchestrator().plan_scenes(
        pack=pack or _FakePack(), db=None, project_id="p", chapter_id="c",
        target_words=target, user_instruction=instruction,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"brief": True}, {"brief": {"action": "归还"}}, {"brief": ["归还"]},
    {"title": False}, {"pov": ["林禾"]}, {"start_state": True},
    {"continuity_ledger": {}}, {"information_state": [False]},
    {"target_words": True}, {"target_words": "1800"}, {"target_words": None},
    {"target_words": 0}, {"target_words": 1800.5}, {"target_words": 7000},
    {"target_words": 300}, {"target_words": 5000},
])
async def test_invalid_planning_is_repaired_before_returning_briefs(monkeypatch, changes):
    invalid = scene(**changes)
    good = scene(brief="宋岚交还书，林禾检查封面后收进柜中。")
    model = AsyncMock(side_effect=[reply([invalid]), reply({"scenes": [good]})])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await plan(instruction="书属于林禾，不新增欠款或损失。")
    assert model.await_count == 2
    assert out[0].brief == good["brief"] and out[0].target_words == 1800
    repair = model.await_args_list[1].args[1]
    assert "书属于林禾，不新增欠款或损失。" in repair
    assert json.dumps([invalid], ensure_ascii=False) in repair
    assert next(iter(changes)) in repair
    assert "不是已发生事实" in repair


@pytest.mark.asyncio
async def test_missing_contract_gets_one_specific_repair(monkeypatch):
    invalid = scene()
    del invalid["entity_transfers"]
    model = AsyncMock(side_effect=[reply([invalid]), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await plan())[0].title == "归还"
    assert model.await_count == 2
    assert "entity_transfers" in model.await_args_list[1].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_fallback", [False, True])
async def test_invalid_known_plan_never_becomes_template_success(monkeypatch, allow_fallback):
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", allow_fallback)
    invalid = scene(brief=False)
    model = AsyncMock(return_value=reply([invalid]))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(RuntimeError, match="scene_planner_failed"):
        await plan()
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter", "tool_calls"])
async def test_abnormal_finish_is_repaired_even_with_complete_json(monkeypatch, finish):
    model = AsyncMock(side_effect=[reply([scene(brief="未确认旧规划")], finish), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await plan())[0].brief == scene()["brief"]
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_truncated_json_is_not_completed_by_local_json_repair(monkeypatch):
    truncated = json.dumps([scene(brief="不完整的规划")], ensure_ascii=False)[:-1]
    model = AsyncMock(side_effect=[reply(truncated), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await plan())[0].brief == scene()["brief"]
    assert model.await_count == 2
    assert truncated in model.await_args_list[1].args[1]


@pytest.mark.asyncio
async def test_duplicate_json_field_cannot_silently_replace_planning(monkeypatch):
    raw = json.dumps([scene()], ensure_ascii=False).replace(
        '"target_words": 1800', '"target_words": 900, "target_words": 1800')
    model = AsyncMock(side_effect=[reply(raw), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    await plan()
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_structured_ledgers_and_conditional_guidance_reach_writer(monkeypatch):
    original = scene(
        information_state={"林禾": {"已知": ["书在宋岚手中"], "未知": ["封面是否修好"]}},
        entity_transfers=[{"物件": "旧书", "从": "宋岚", "到": "林禾"}],
        time_delta=0,
        action_budget="", inference_ledger=None,
        actor_boundary="宋岚看见林禾担心，才主动翻开修好的封面。",
        npc_disclosure_budget="林禾告诉宋岚书是母亲留下的，因为宋岚问起修补方式。",
        evidence_strength={"封面": "可当面查看"},
        foreshadow_seed="本章不另埋伏笔", register_guard="两人用熟人之间的日常口吻说话",
    )
    before = copy.deepcopy(original)
    monkeypatch.setattr(prompt_registry, "run_text_prompt", AsyncMock(return_value=reply([original])))
    out = await plan()
    assert original == before
    assert json.loads(out[0].information_state) == original["information_state"]
    assert json.loads(out[0].entity_transfers) == original["entity_transfers"]
    assert out[0].time_delta == "0"
    rendered = out[0].to_writer_user_content()
    for field in ("actor_boundary", "npc_disclosure_budget", "foreshadow_seed", "register_guard"):
        assert original[field] in rendered
    assert "可当面查看" in rendered


@pytest.mark.asyncio
async def test_reallocation_preserves_all_events_and_author_request(monkeypatch):
    invalid = [scene(title=f"场{i}", brief=f"原定事件{i}", target_words=900) for i in range(8)]
    repaired = [{**s, "target_words": 375} for s in invalid]
    model = AsyncMock(side_effect=[reply(invalid), reply(repaired)])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    pack = _FakePack("作者已经确认的完整前文。" * 500)
    instruction = "本章按次序处理八次来访，最后宋岚关店。"
    out = await plan(target=3000, pack=pack, instruction=instruction)
    assert [s.brief for s in out] == [s["brief"] for s in invalid]
    assert sum(s.target_words for s in out) == 3000
    assert model.await_count == 2
    repair = model.await_args_list[1]
    assert instruction in repair.args[1] and json.dumps(invalid, ensure_ascii=False) in repair.args[1]
    assert pack.to_system_prompt() == repair.args[3]
    assert "不删除中间事件" in repair.args[1]


@pytest.mark.asyncio
async def test_failed_planning_emits_no_scene_text(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", False)
    monkeypatch.setattr(SceneOrchestrator, "_assert_outline_chain_ready", AsyncMock())
    monkeypatch.setattr(scene_orchestrator, "ContextPackBuilder",
                        lambda **kw: SimpleNamespace(build=AsyncMock(return_value=_FakePack())))
    model = AsyncMock(return_value=reply([scene(brief=True)]))
    writer = Mock(side_effect=AssertionError("invalid planning must not start writer"))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    monkeypatch.setattr(scene_orchestrator, "stream_text_prompt", writer)
    delivered = []
    with pytest.raises(RuntimeError, match="scene_planner_failed"):
        async for chunk in SceneOrchestrator().orchestrate_chapter_stream(
            project_id="p", volume_id="v", chapter_idx=1, db=None, target_words=1800,
        ):
            delivered.append(chunk)
    assert delivered == [] and writer.call_count == 0 and model.await_count == 2


@pytest.mark.asyncio
async def test_repair_timeout_does_not_exceed_configured_hard_cap(monkeypatch):
    monkeypatch.setattr(settings, "SCENE_PLANNER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(settings, "SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS", 20.0)
    timeouts = []

    async def bounded(coro, timeout):
        timeouts.append(timeout)
        return await coro

    monkeypatch.setattr(asyncio, "wait_for", bounded)
    monkeypatch.setattr(prompt_registry, "run_structured_prompt", AsyncMock(
        side_effect=[{"parse_error": True, "raw_text": "broken"}, {"items": [scene()]}]))
    await plan()
    assert timeouts == [10.0, 10.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [0, -1, True, 200000])
async def test_invalid_or_unachievable_target_fails_before_model(monkeypatch, target):
    model = AsyncMock(return_value=reply([scene(target_words=6000) for _ in range(20)]))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(RuntimeError, match="scene_planner_failed"):
        await plan(target=target)
    assert model.await_count == 0


@pytest.mark.asyncio
async def test_cancellation_does_not_launch_repair(monkeypatch):
    model = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(asyncio.CancelledError):
        await plan()
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_known_plan_is_not_discarded_after_repair_transport_failure(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    model = AsyncMock(side_effect=[reply([scene(brief=False)]), RuntimeError("unavailable")])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(RuntimeError, match="invalid_scene_field"):
        await plan()
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_incomplete_output_cannot_be_replaced_by_emergency_template(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    model = AsyncMock(return_value=reply([scene()], "length"))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(RuntimeError, match="provider_incomplete_response"):
        await plan()
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["items", "scenes", "scene_briefs", "scene_list"])
async def test_supported_scene_wrappers_keep_canonical_order(monkeypatch, wrapper):
    scenes = [scene(idx=99, brief="先归还旧书。", target_words=1500),
              scene(idx=-1, brief="再约定次日下午见面。", target_words=300)]
    model = AsyncMock(return_value=reply({wrapper: scenes}))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await plan()
    assert [s.idx for s in out] == [1, 2]
    assert [s.brief for s in out] == [s["brief"] for s in scenes]
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_wrappers_cannot_drop_the_other_sequence(monkeypatch):
    invalid = {"items": [scene(brief="第一份安排")], "scenes": [scene(brief="另一份安排")]}
    model = AsyncMock(side_effect=[reply(invalid), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    await plan()
    repair = model.await_args_list[1].args[1]
    assert "第一份安排" in repair and "另一份安排" in repair
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1530, 2070])
async def test_declared_budget_endpoints_are_inclusive(monkeypatch, budget):
    model = AsyncMock(return_value=reply([scene(target_words=budget)]))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    assert (await plan())[0].target_words == budget
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_continuity_aliases_are_preserved_without_inventing_missing_state(monkeypatch):
    original = scene()
    del original["continuity_ledger"]
    del original["transition_bridge"]
    original["连续性台账"] = {"旧书": {"持有人": "宋岚", "计划交给": "林禾", "册数": 1}}
    original["handoff_to_next"] = "林禾将书收入柜中，两人仍在店里。"
    model = AsyncMock(return_value=reply([original]))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await plan()
    assert json.loads(out[0].continuity_ledger) == original["连续性台账"]
    assert out[0].transition_bridge == original["handoff_to_next"]
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_conflicting_aliases_are_repaired_without_ignoring_either_version(monkeypatch):
    invalid = scene(continuity_ledger="旧书在林禾手中", ledger="旧书仍在宋岚手中")
    model = AsyncMock(side_effect=[reply([invalid]), reply([scene()])])
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    await plan()
    assert model.await_count == 2
    assert "conflicting_scene_fields" in model.await_args_list[1].args[1]
    assert "旧书在林禾手中" in model.await_args_list[1].args[1]
    assert "旧书仍在宋岚手中" in model.await_args_list[1].args[1]


@pytest.mark.asyncio
async def test_identical_json_aliases_do_not_conflict_over_key_order(monkeypatch):
    original = scene(continuity_ledger={"物件": "旧书", "持有人": "林禾"},
                     ledger={"持有人": "林禾", "物件": "旧书"})
    model = AsyncMock(return_value=reply([original]))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    out = await plan()
    assert json.loads(out[0].continuity_ledger) == original["continuity_ledger"]
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["parse_error", "_parse_error"])
async def test_model_error_flag_cannot_hide_existing_plan_from_failure_guard(monkeypatch, flag):
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    model = AsyncMock(return_value=reply({flag: True, "scenes": [scene()]}))
    monkeypatch.setattr(prompt_registry, "run_text_prompt", model)
    with pytest.raises(RuntimeError, match="scene_planner_failed"):
        await plan()
    assert model.await_count == 2


def test_recommended_scene_count_limits_single_generation_output():
    """单场输出上限决定场数,而不是"情节自然转折"决定。

    一章只写一场时,整章就是一次生成,而每场"重新注入完整上下文 + 本章已写正文"
    的分段机制完全用不上;一次生成越长,开头的数量/时间/称谓约束越容易在中后段失效。
    """
    from app.services.scene_plan_contract import recommended_scene_count
    assert recommended_scene_count(1200, 6000) == 1   # 默认:维持现状
    assert recommended_scene_count(1200, 600) == 2
    assert recommended_scene_count(3000, 600) == 5
    assert recommended_scene_count(3500, 600) == 6
    assert recommended_scene_count(400, 600) == 1
    assert recommended_scene_count(12000, 600) == 20  # 上限 MAX_SCENE_COUNT
    assert recommended_scene_count(1200, 0) == 1      # 非法上限不炸,退化成 1 场


def test_planner_prompt_states_the_single_scene_output_ceiling(monkeypatch):
    """上限必须写进规划提示,否则规划器仍然会把整章塞成一场。"""
    from app.services import prompt_registry
    from app.services.scene_plan_contract import recommended_scene_count
    captured = {}

    async def fake_call(*, task_type, user_content, **kw):
        captured["user"] = user_content
        return {"scenes": []}

    monkeypatch.setattr(prompt_registry, "run_structured_prompt", fake_call, raising=False)
    monkeypatch.setattr(settings, "SCENE_MAX_OUTPUT_WORDS", 600, raising=False)
    try:
        asyncio.run(SceneOrchestrator().plan_scenes(
            pack=_FakePack(), db=None, project_id="p", chapter_id=None, target_words=1200))
    except Exception:  # 规划结果本身可能不合法;这里只关心提示文本
        pass
    text = captured.get("user", "")
    assert "单场上限 600 字" in text
    assert f"推荐场景数 = {recommended_scene_count(1200, 600)}" in text
