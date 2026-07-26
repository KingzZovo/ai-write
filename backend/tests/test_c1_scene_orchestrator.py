"""v1.5.0 C1 — SceneOrchestrator regression suite.

Covers:
  1. SceneBrief.from_dict clamps target_words into [800, 1200] and falls
     back on empty/garbage inputs.
  2. _try_parse_scene_array handles strict JSON, fenced ```json blocks,
     wrapped {"scenes": [...]} dict shape, and returns None for garbage.
  3. _fallback_scene_briefs yields enough 800-1200 word scenes for long chapter targets,
     bounds and last brief has empty hook (chapter end).
  4. SceneBrief.to_writer_user_content emits expected anchor strings.
  5. SceneOrchestrator.plan_scenes uses LLM result when JSON parse succeeds.
  6. SceneOrchestrator.plan_scenes falls back when LLM returns garbage.
  7. SceneOrchestrator.plan_scenes falls back when LLM raises an exception.
  8. SceneOrchestrator.orchestrate_chapter_stream emits a \n\n separator
     between scenes and concatenates per-scene streams in order.
  9. orchestrate_chapter_stream invokes on_scene_start callback exactly
     N times with the SceneBrief instances in order.
 10. TASK_TYPE_RECOMMENDATIONS includes scene_planner (standard) +
     scene_writer (flagship).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from app.services.prompt_recommendations import TASK_TYPE_RECOMMENDATIONS
from app.services.scene_orchestrator import (
    MAX_SCENE_WORDS,
    MIN_SCENE_WORDS,
    SceneBrief,
    SceneOrchestrator,
    _fallback_scene_briefs,
    _has_valid_scene_contract,
    _try_parse_scene_array,
)


class _FakePack:
    """Minimal stand-in for ContextPack used in scene tests."""

    def __init__(self, system_prompt: str = "<world rules>\n<chapter outline body>") -> None:
        self._system_prompt = system_prompt
        # Fix1 roster injection reads pack.character_cards; real ContextPack
        # always defines it, so the fake mirrors that contract.
        self.character_cards: list = []

    def to_system_prompt(self) -> str:
        return self._system_prompt

    def to_messages(self, user_instruction: str = "") -> list[dict]:
        msgs = [{"role": "system", "content": self._system_prompt}]
        msgs.append({"role": "user", "content": user_instruction or "生成"})
        return msgs


async def _async_iter(chunks):
    for c in chunks:
        yield c


def _scene_contract(**overrides) -> dict:
    """Generic scene-contract fields required by current planner gate."""
    base = {
        "title": "场",
        "brief": "推进主线",
        "pov": "A",
        "target_words": 900,
        "hook": "接下一场",
        "start_state": "承接上一场末尾状态",
        "time_delta": "紧接上一场，未跳过关键耗时",
        "location_path": "同地承接或写清移动路径",
        "entity_transfers": "人物/物件/消息按场初台账到场",
        "power_resource_map": "双方资源差与代价清楚",
        "information_state": "角色只使用已知信息",
        "mechanism_limits": "改变局势的机制有触发条件和边界",
        "result_strength": "只允许局部推进，支撑不足降级",
        "transition_bridge": "把场末状态交给下一场",
        "continuity_ledger": "人物/物件/消息/证据/资源：场初 -> 场末，写清持有人/知情人/路径/代价",
        "action_budget": "高压窗口、身体姿态、双手限制、预先准备、最多连续动作数和代价清楚",
        "inference_ledger": "感知来源/证据、可推出结论强度、替代解释和允许写法清楚",
    }
    base.update(overrides)
    return base


# 1) ---------- SceneBrief.from_dict clamping ----------

def test_scene_brief_target_words_clamps_high():
    b = SceneBrief.from_dict(1, {"title": "t", "brief": "b", "target_words": 5000})
    assert b.target_words == MAX_SCENE_WORDS


def test_scene_brief_target_words_clamps_low():
    b = SceneBrief.from_dict(2, {"title": "t", "brief": "b", "target_words": 200})
    assert b.target_words == MIN_SCENE_WORDS


def test_scene_brief_handles_missing_target_words():
    b = SceneBrief.from_dict(3, {"title": "t", "brief": "b"})
    assert b.target_words == 1000  # default


def test_scene_brief_default_title_when_empty():
    b = SceneBrief.from_dict(4, {"brief": "b"})
    assert b.title == "场景 4"


# 2) ---------- _try_parse_scene_array ----------

def test_parse_strict_json_array():
    out = _try_parse_scene_array('[{"title":"x"}]')
    assert out == [{"title": "x"}]


def test_parse_fenced_json_block():
    raw = '以下是场景计划：\n```json\n[{"idx":1,"title":"a"}]\n```\n'
    out = _try_parse_scene_array(raw)
    assert out == [{"idx": 1, "title": "a"}]


def test_parse_wrapped_scenes_dict():
    out = _try_parse_scene_array('{"scenes": [{"title": "x"}]}')
    assert out == [{"title": "x"}]


def test_parse_garbage_returns_none():
    assert _try_parse_scene_array("hello world") is None
    assert _try_parse_scene_array("") is None
    assert _try_parse_scene_array("[1, 2, 3]") is None  # not list[dict]


# 2b) ---------- _cap_parsed_scenes hard-cap behaviour ----------

def test_planner_overflow_keeps_tail_scene():
    from app.services.scene_orchestrator import MAX_SCENE_COUNT, _cap_parsed_scenes

    parsed = [{"goal": f"s{i}"} for i in range(MAX_SCENE_COUNT + 5)]
    capped = _cap_parsed_scenes(parsed)
    assert len(capped) == MAX_SCENE_COUNT
    assert capped[-1]["goal"] == f"s{MAX_SCENE_COUNT + 4}"  # 结尾场景必须保留


def test_planner_within_cap_untouched():
    from app.services.scene_orchestrator import _cap_parsed_scenes

    parsed = [{"goal": f"s{i}"} for i in range(8)]
    assert _cap_parsed_scenes(parsed) == parsed


# 3) ---------- _fallback_scene_briefs ----------

@pytest.mark.parametrize(
    "target,expected_min_n,expected_max_n",
    [
        (3000, 3, 3),
        (3500, 3, 4),
        (5000, 4, 5),
        (6500, 6, 7),
        (10000, 9, 10),
        (12500, 12, 13),
    ],
)
def test_fallback_scene_count_scales(target, expected_min_n, expected_max_n):
    out = _fallback_scene_briefs(target, "A" * 800)
    assert expected_min_n <= len(out) <= expected_max_n, (target, len(out))
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


def test_fallback_scene_briefs_scale_to_12500_target_without_six_scene_cap():
    out = _fallback_scene_briefs(12500, "A" * 2400)

    assert len(out) >= 12
    assert sum(b.target_words for b in out) >= 12500 * 0.85
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


def test_fallback_last_scene_hook_empty():
    out = _fallback_scene_briefs(4000, "x" * 500)
    assert out[-1].hook == ""
    assert all(b.hook for b in out[:-1])


def test_fallback_scene_briefs_fill_action_budget_and_inference_ledger():
    out = _fallback_scene_briefs(4000, "x" * 500)
    assert out
    assert all(b.action_budget for b in out)
    assert all(b.inference_ledger for b in out)


# 4) ---------- SceneBrief.to_writer_user_content ----------

def test_writer_user_content_contains_all_anchors():
    b = SceneBrief.from_dict(
        2,
        {
            "title": "雨夜",
            "brief": "主角混入禁库",
            "pov": "路明非",
            "location": "禁库",
            "time_cue": "雨夜",
            "key_action": "探查头骨",
            "target_words": 1100,
            "hook": "狂躯裂开",
        },
    )
    uc = b.to_writer_user_content()
    for needle in ["第 2 场", "雨夜", "路明非", "禁库", "探查头骨", "800-1200", "狂躯裂开"]:
        assert needle in uc, (needle, uc)


def test_writer_user_content_empty_hook_marks_chapter_end():
    b = SceneBrief.from_dict(3, {"title": "末场", "brief": "b"})
    uc = b.to_writer_user_content()
    assert "本场为末场" in uc


def test_scene_contract_validator_rejects_missing_ledger():
    bad = [SceneBrief.from_dict(1, {"title": "缺台账", "brief": "b"})]
    assert _has_valid_scene_contract(bad) is False


def test_scene_contract_validator_accepts_missing_conditional_fields():
    # action_budget / inference_ledger are conditional (high-pressure scenes
    # only) per SCENE_CONTRACT_FIELDS_PROMPT; a calm scene may omit them and
    # must still pass the gate (otherwise a good plan is forced to fallback).
    missing_action = _scene_contract(action_budget="")
    missing_inference = _scene_contract(inference_ledger="")
    assert _has_valid_scene_contract([SceneBrief.from_dict(1, missing_action)]) is True
    assert _has_valid_scene_contract([SceneBrief.from_dict(1, missing_inference)]) is True


def test_scene_contract_validator_accepts_complete_generic_contract():
    good = [SceneBrief.from_dict(1, _scene_contract())]
    assert _has_valid_scene_contract(good) is True


def test_scene_brief_preserves_continuity_and_action_inference_ledgers_in_writer_content():
    b = SceneBrief.from_dict(
        1,
        _scene_contract(
            continuity_ledger="甲持证据：门外 -> 门内，乙知情",
            action_budget="近身搜拿窗口只能完成一动作，需付出受伤代价",
            inference_ledger="只看见半圈白痕 -> 只能判断硬物压痕 -> 不得定案",
        ),
    )
    uc = b.to_writer_user_content()
    assert "【连续性台账】" in uc
    assert "【动作预算】" in uc
    assert "【推理台账】" in uc
    assert "甲持证据" in uc
    assert "近身搜拿窗口" in uc
    assert "半圈白痕" in uc


# 5) ---------- SceneOrchestrator.plan_scenes happy path ----------

@pytest.mark.asyncio
async def test_plan_scenes_uses_llm_when_json_parses():
    pack = _FakePack()
    fake_briefs = [
        _scene_contract(title="起", brief="起头", target_words=900, hook="h1"),
        _scene_contract(title="承", brief="转折", target_words=1100, hook="h2"),
        _scene_contract(title="转", brief="高潮", target_words=1100, hook="h3"),
        _scene_contract(title="合", brief="收尾", target_words=900, hook=""),
    ]
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": fake_briefs}),
    ) as mocked:
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack,
            db=None,
            project_id="p1",
            chapter_id="c1",
            target_words=4000,
            n_scenes_hint=4,
        )
        assert mocked.await_count == 1
        assert mocked.call_args.kwargs["task_type"] == "scene_planner"
    assert len(out) == 4
    assert out[0].title == "起" and out[3].hook == ""
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


@pytest.mark.asyncio
async def test_plan_scenes_keeps_more_than_six_scenes_for_long_targets():
    pack = _FakePack()
    fake_briefs = [
        _scene_contract(title=f"场{i}", brief=f"推进{i}", target_words=1000, hook="接下一场")
        for i in range(1, 13)
    ]
    fake_briefs[-1]["hook"] = ""
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": fake_briefs}),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack,
            db=None,
            project_id="p1",
            chapter_id="c1",
            target_words=12500,
            n_scenes_hint=12,
        )

    assert len(out) == 12
    assert out[-1].hook == ""


@pytest.mark.asyncio
async def test_plan_scenes_keeps_all_scenes_when_planner_exceeds_soft_hint():
    """B7 regression: the soft per-target hint must not truncate planner output.

    target_words=3000 yields a soft hint of 3 scenes; if the planner returns
    8 well-formed scenes, all 8 must survive — especially the final scene,
    which carries the chapter ending/hook.
    """
    pack = _FakePack()
    fake_briefs = [
        _scene_contract(title=f"场{i}", brief=f"推进{i}", target_words=900, hook="接下一场")
        for i in range(1, 9)
    ]
    fake_briefs[-1]["title"] = "终场"
    fake_briefs[-1]["hook"] = ""
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": fake_briefs}),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack,
            db=None,
            project_id="p1",
            chapter_id="c1",
            target_words=3000,
            n_scenes_hint=None,
        )

    assert len(out) == 8
    assert out[-1].title == "终场"
    assert out[-1].hook == ""


# 6) ---------- plan_scenes falls back on garbage ----------

@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_unparseable():
    pack = _FakePack()
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(
            return_value={
                "raw_text": "sorry I cannot do this",
                "parse_error": "not JSON",
            }
        ),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack, db=None, project_id="p", chapter_id=None,
            target_words=3500, n_scenes_hint=None,
        )
    assert 3 <= len(out) <= 6
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


@pytest.mark.asyncio
async def test_plan_scenes_falls_back_when_contract_fields_missing():
    pack = _FakePack("<world rules>\n<chapter outline body>" * 20)
    incomplete_briefs = [
        {"title": "起", "brief": "缺少台账", "target_words": 900, "hook": "h1"},
        _scene_contract(title="承", brief="完整", target_words=900, hook="h2"),
        _scene_contract(title="合", brief="完整", target_words=900, hook=""),
    ]
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": incomplete_briefs}),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack,
            db=None,
            project_id="p",
            chapter_id="c",
            target_words=3500,
            n_scenes_hint=3,
        )
    assert 3 <= len(out) <= 6
    assert all(b.title.startswith("场景 ") for b in out)
    assert all(b.continuity_ledger for b in out)


# 7) ---------- plan_scenes falls back on LLM exception ----------

@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_llm_exception():
    pack = _FakePack()
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(side_effect=RuntimeError("upstream 500")),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack, db=None, project_id="p", chapter_id=None,
            target_words=3500, n_scenes_hint=None,
        )
    assert 3 <= len(out) <= 6


# 8) ---------- orchestrate_chapter_stream concatenates scene streams ----------

@pytest.mark.asyncio
async def test_orchestrate_concatenates_scenes_with_separator():
    pack = _FakePack()
    briefs_json = json.dumps([
        _scene_contract(title="a", brief="x", target_words=900, hook="h"),
        _scene_contract(title="b", brief="y", target_words=900, hook="h"),
        _scene_contract(title="c", brief="z", target_words=900, hook=""),
    ], ensure_ascii=False)
    # Each scene yields three chunks; we expect them joined with \n\n
    # separators between scenes (no leading separator).
    scene_chunks = {
        1: ["场1-块A", "场1-块B", "场1-块C"],
        2: ["场2-块A", "场2-块B", "场2-块C"],
        3: ["场3-块A", "场3-块B", "场3-块C"],
    }
    call_count = {"n": 0}

    def _stream(*args, **kwargs):
        call_count["n"] += 1
        return _async_iter(scene_chunks[call_count["n"]])

    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value=json.loads(briefs_json)),
    ), patch(
        "app.services.scene_orchestrator.stream_text_prompt",
        side_effect=_stream,
    ), patch(
        "app.services.scene_orchestrator.ContextPackBuilder",
    ) as mocked_pack_builder:
        # ContextPackBuilder(db=db).build(...) -> pack
        instance = mocked_pack_builder.return_value
        instance.build = AsyncMock(return_value=pack)

        orch = SceneOrchestrator()
        chunks: list[str] = []
        async for c in orch.orchestrate_chapter_stream(
            project_id="p", volume_id="v", chapter_idx=1,
            db=None, chapter_id="c",
            target_words=2700,
        ):
            chunks.append(c)

    full = "".join(chunks)
    assert "场1-块A场1-块B场1-块C\n\n场2-块A场2-块B场2-块C\n\n场3-块A场3-块B场3-块C" == full, full
    assert call_count["n"] == 3


# 9) ---------- on_scene_start callback ----------

@pytest.mark.asyncio
async def test_on_scene_start_callback_called_per_scene():
    pack = _FakePack()
    briefs_json = json.dumps([
        _scene_contract(title=f"场 {i}", brief="b", target_words=900, hook="h" if i < 3 else "")
        for i in (1, 2, 3)
    ], ensure_ascii=False)
    seen: list[int] = []

    async def cb(scene):
        seen.append(scene.idx)

    def _stream(*args, **kwargs):
        return _async_iter(["x"])

    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value=json.loads(briefs_json)),
    ), patch(
        "app.services.scene_orchestrator.stream_text_prompt",
        side_effect=_stream,
    ), patch(
        "app.services.scene_orchestrator.ContextPackBuilder",
    ) as mocked_pack_builder:
        mocked_pack_builder.return_value.build = AsyncMock(return_value=pack)
        orch = SceneOrchestrator()
        async for _ in orch.orchestrate_chapter_stream(
            project_id="p", volume_id="v", chapter_idx=1,
            db=None, chapter_id=None,
            target_words=2700, on_scene_start=cb,
        ):
            pass
    assert seen == [1, 2, 3]


# 10) ---------- TASK_TYPE_RECOMMENDATIONS registration ----------

def test_scene_task_types_are_registered():
    assert TASK_TYPE_RECOMMENDATIONS["scene_planner"]["tier"] == "standard"
    assert TASK_TYPE_RECOMMENDATIONS["scene_planner"]["kind"] == "chat"
    assert TASK_TYPE_RECOMMENDATIONS["scene_writer"]["tier"] == "flagship"
    assert TASK_TYPE_RECOMMENDATIONS["scene_writer"]["kind"] == "chat"
