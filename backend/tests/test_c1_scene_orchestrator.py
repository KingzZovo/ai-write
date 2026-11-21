"""v1.5.0 C1 — SceneOrchestrator regression suite.

Covers:
  1. SceneBrief.from_dict rejects absent/invalid scene budgets instead of
     silently clamping them or inventing a default.
  2. _try_parse_scene_array handles strict JSON, fenced ```json blocks,
     wrapped {"scenes": [...]} dict shape, and returns None for garbage.
  3. _fallback_scene_briefs yields enough budgeted scenes for long chapter targets,
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
    MAX_SCENE_COUNT,
    MAX_SCENE_WORDS,
    MIN_SCENE_WORDS,
    SceneBrief,
    SceneOrchestrator,
    _fallback_scene_briefs,
    _has_valid_scene_contract,
    _scene_budget_reaches_target,
    _try_parse_scene_array,
)


class _FakePack:
    """Minimal stand-in for ContextPack used in scene tests."""

    def __init__(self, system_prompt: str = "<world rules>\n<chapter outline body>") -> None:
        self._system_prompt = system_prompt
        self.current_outline = {"summary": "当前章明确细纲"}
        # Fix1 roster injection reads pack.character_cards; real ContextPack
        # always defines it, so the fake mirrors that contract.
        self.character_cards: list = []

    def to_system_prompt(self) -> str:
        return self._system_prompt

    async def resolve_cognition(self, **kwargs):
        pass

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


# 1) ---------- SceneBrief.from_dict budget validation ----------

def test_scene_brief_rejects_excessive_target_words():
    with pytest.raises(ValueError, match="target_words"):
        SceneBrief.from_dict(1, {"title": "t", "brief": "b", "target_words": 60000})


def test_scene_brief_rejects_zero_target_words():
    with pytest.raises(ValueError, match="target_words"):
        SceneBrief.from_dict(2, {"title": "t", "brief": "b", "target_words": 0})


def test_scene_brief_handles_missing_target_words():
    with pytest.raises(ValueError, match="target_words"):
        SceneBrief.from_dict(3, {"title": "t", "brief": "b"})


def test_scene_brief_default_title_when_empty():
    b = SceneBrief.from_dict(4, {"brief": "b", "target_words": 1000})
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

def test_planner_overflow_rejects_instead_of_dropping_middle_scenes():
    from app.services.scene_orchestrator import MAX_SCENE_COUNT, _cap_parsed_scenes

    parsed = [{"goal": f"s{i}"} for i in range(MAX_SCENE_COUNT + 5)]
    with pytest.raises(ValueError, match="scene_count_exceeds_limit"):
        _cap_parsed_scenes(parsed)
    assert len(parsed) == MAX_SCENE_COUNT + 5


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
    for needle in ["第 2 场", "雨夜", "路明非", "禁库", "探查头骨", "【目标字数】", "狂躯裂开"]:
        assert needle in uc, (needle, uc)


def test_writer_user_content_empty_hook_does_not_invent_chapter_end_or_suspense():
    b = SceneBrief.from_dict(3, {"title": "末场", "brief": "b", "target_words": 1000})
    uc = b.to_writer_user_content()
    assert "本场为末场" not in uc
    assert "需送入本章钩子" not in uc
    assert "不据此提前写全章结尾或新增悬念" in uc


def test_scene_contract_validator_rejects_missing_ledger():
    bad = [SceneBrief.from_dict(1, {"title": "缺台账", "brief": "b", "target_words": 1000})]
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
        _scene_contract(title=f"场{i}", brief=f"推进{i}", target_words=375, hook="接下一场")
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
async def test_plan_scenes_falls_back_on_unparseable(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
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
async def test_plan_scenes_rejects_missing_contract_after_repair_even_with_fallback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    pack = _FakePack("<world rules>\n<chapter outline body>" * 20)
    incomplete_briefs = [
        {"title": "起", "brief": "缺少台账", "target_words": 900, "hook": "h1"},
        _scene_contract(title="承", brief="完整", target_words=900, hook="h2"),
        _scene_contract(title="合", brief="完整", target_words=900, hook=""),
    ]
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": incomplete_briefs}),
    ) as calls:
        orch = SceneOrchestrator()
        with pytest.raises(RuntimeError, match="missing_contract_fields"):
            await orch.plan_scenes(
                pack=pack, db=None, project_id="p", chapter_id="c",
                target_words=3500, n_scenes_hint=3,
            )
    assert calls.await_count == 2


# 7) ---------- plan_scenes falls back on LLM exception ----------

@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_llm_exception(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
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


@pytest.mark.asyncio
async def test_plan_scenes_no_usable_output_counts_fallback_metric(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    """The no-usable-output branch must increment scene_plan_fallback_total
    like the other three fallback branches (too_few / insufficient_scene_budget
    / missing_contract_fields)."""
    from app.observability.metrics import SCENE_PLAN_FALLBACK_TOTAL

    def _val() -> float:
        try:
            return float(
                SCENE_PLAN_FALLBACK_TOTAL.labels(reason="no_usable_output")._value.get()
            )
        except Exception:
            return 0.0

    before = _val()
    pack = _FakePack()
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(
            return_value={"raw_text": "not json at all", "parse_error": True}
        ),
    ):
        orch = SceneOrchestrator()
        await orch.plan_scenes(
            pack=pack, db=None, project_id="p", chapter_id=None,
            target_words=3500, n_scenes_hint=None,
        )
    assert _val() == before + 1  # the branch runs once per plan_scenes call


# 7b) ---------- scene budget lower and upper bounds ----------

def _briefs_with_budget(n: int, per_scene: int) -> list[SceneBrief]:
    return [
        SceneBrief(idx=i, title=f"场{i}", brief="推进", target_words=per_scene)
        for i in range(1, n + 1)
    ]


def test_scene_budget_cannot_expand_chapter_fourfold():
    briefs = _briefs_with_budget(MAX_SCENE_COUNT, MAX_SCENE_WORDS)
    assert _scene_budget_reaches_target(briefs, 30000) is False
    assert _scene_budget_reaches_target(briefs, 120000) is True


def test_scene_budget_small_plan_still_fails_large_target():
    briefs = _briefs_with_budget(3, MAX_SCENE_WORDS)  # Still less than 85% of 30000 at the current per-scene ceiling
    assert _scene_budget_reaches_target(briefs, 30000) is False


def test_scene_budget_unclamped_below_cap():
    # Requirement 0.85 * 4000 = 3400 is achievable: no clamping applies.
    assert _scene_budget_reaches_target(_briefs_with_budget(4, 850), 4000) is True
    assert _scene_budget_reaches_target(_briefs_with_budget(3, 1000), 4000) is False


@pytest.mark.asyncio
async def test_plan_scenes_accepts_valid_20_scene_plan_for_30000_target():
    """A long chapter can still use 20 complete scene contracts."""
    pack = _FakePack()
    fake_briefs = [
        _scene_contract(
            title=f"场{i}", brief=f"推进{i}", target_words=1500,
            hook="接下一场",
        )
        for i in range(1, MAX_SCENE_COUNT + 1)
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
            project_id="p",
            chapter_id="c",
            target_words=30000,
            n_scenes_hint=MAX_SCENE_COUNT,
        )
    assert len(out) == MAX_SCENE_COUNT
    # Planner titles survive — template fallback would rename to "场景 N".
    assert [b.title for b in out] == [f"场{i}" for i in range(1, MAX_SCENE_COUNT + 1)]
    assert out[-1].hook == ""


# 8) ---------- orchestrate_chapter_stream concatenates scene streams ----------

@pytest.mark.asyncio
async def test_orchestrate_concatenates_scenes_with_separator(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(SceneOrchestrator, "_assert_outline_chain_ready", AsyncMock())
    # 本用例只验证"场与场之间怎么拼";每场篇幅兜底会额外发续写调用并追加正文,
    # 那是另一条性质,单独由 tests/test_scene_length_continuation.py 覆盖。
    monkeypatch.setattr(settings, "SCENE_LENGTH_CONTINUATION_ENABLED", False, raising=False)
    # 同理关掉**章级**补足:本用例三场一共 27 字,远低于章目标,否则会再发一次补足
    # 调用(那是 tests/test_chapter_length_topup.py 的覆盖范围)。
    monkeypatch.setattr(settings, "CHAPTER_LENGTH_CONTINUATION_MAX", 0, raising=False)
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
async def test_on_scene_start_callback_called_per_scene(monkeypatch):
    monkeypatch.setattr(SceneOrchestrator, "_assert_outline_chain_ready", AsyncMock())
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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["overflow", "non_object"])
async def test_invalid_scene_sequence_is_replanned_without_silent_loss(invalid_kind):
    valid = [_scene_contract(title=f"scene{i}", brief=f"event{i}", target_words=1000, hook="") for i in range(3)]
    invalid = valid * 7 if invalid_kind == "overflow" else [*valid, "critical missing scene"]
    runner = AsyncMock(side_effect=[{"scenes": invalid}, {"scenes": valid}])
    with patch("app.services.prompt_registry.run_structured_prompt", new=runner):
        out = await SceneOrchestrator().plan_scenes(pack=_FakePack(), db=None,
            project_id="p", chapter_id="c", target_words=3000)
    assert [b.title for b in out] == [s["title"] for s in valid]
    assert runner.await_count == 2
    repair = runner.call_args.kwargs["user_content"]
    assert "不删除中间事件" in repair
    assert json.dumps(invalid, ensure_ascii=False) in repair


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_fallback", [True, False])
async def test_exhausted_overflow_never_turns_into_template_success(monkeypatch, allow_fallback):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", allow_fallback)
    invalid = [_scene_contract(title=str(i), brief="转折", target_words=1000, hook="") for i in range(MAX_SCENE_COUNT+1)]
    runner = AsyncMock(return_value={"scenes": invalid})
    with patch("app.services.prompt_registry.run_structured_prompt", new=runner):
        with pytest.raises(RuntimeError, match="scene_count_exceeds_limit"):
            await SceneOrchestrator().plan_scenes(pack=_FakePack(), db=None,
                project_id="p", chapter_id="c", target_words=3000)
    assert runner.await_count == 2


def test_fallback_is_opt_in_by_default():
    from app.config import Settings
    assert Settings.model_fields["ALLOW_SCENE_PLANNER_FALLBACK"].default is False


def test_outline_seed_preserves_nested_current_contract_without_reading_prompt():
    pack = _FakePack("未来参考与文风，不是本章剧情" * 1000)
    pack.current_outline = {"summary": "开头事实" + "关键内容" * 1000 + "结局兑现",
        "scenes": [{"action_budget": {"hands": "仅右手", "cost": "销毁唯一铜铃"}}]}
    pack.to_system_prompt = lambda: (_ for _ in ()).throw(AssertionError("must not read assembled prompt"))
    seed = SceneOrchestrator._extract_chapter_outline_text(pack)
    assert json.loads(seed) == pack.current_outline
    assert "未来参考" not in seed
    fallback = _fallback_scene_briefs(4000, seed)
    assert "".join(scene.brief for scene in fallback) == seed


@pytest.mark.asyncio
async def test_missing_current_outline_cannot_seed_emergency_from_other_context(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", True)
    pack = _FakePack("把风格说明误当剧情")
    pack.current_outline = {}
    with patch("app.services.prompt_registry.run_structured_prompt", new=AsyncMock(return_value={"parse_error": True})):
        with pytest.raises(RuntimeError, match="missing_current_outline_for_fallback"):
            await SceneOrchestrator().plan_scenes(pack=pack, db=None, project_id="p", chapter_id="c", target_words=3000)


@pytest.mark.asyncio
async def test_strict_planning_failure_does_not_return_template(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALLOW_SCENE_PLANNER_FALLBACK", False)
    with patch("app.services.prompt_registry.run_structured_prompt", new=AsyncMock(side_effect=RuntimeError("unavailable"))):
        with pytest.raises(RuntimeError, match="scene_planner_failed"):
            await SceneOrchestrator().plan_scenes(pack=_FakePack(), db=None, project_id="p", chapter_id="c", target_words=3000)


@pytest.mark.asyncio
@pytest.mark.parametrize('budgets', [[1800], [1500, 300]])
async def test_short_chapter_accepts_natural_scene_count_and_budgets(budgets):
    plan = [_scene_contract(title=f'场{i}', brief='同一车站内继续谈清交接。',
             target_words=budget, location='值班室', time_cue='十八点后')
            for i, budget in enumerate(budgets, 1)]
    with patch('app.services.prompt_registry.run_structured_prompt', new=AsyncMock(return_value={'scenes': plan})) as call:
        briefs = await SceneOrchestrator().plan_scenes(pack=_FakePack(), db=None,
            project_id='p', chapter_id='c', target_words=1800, n_scenes_hint=1)
    assert [b.target_words for b in briefs] == budgets
    assert len(briefs) == len(budgets)
    prompt = call.call_args.kwargs['user_content']
    assert '推荐场景数 = 1' in prompt
    assert '同一地点、同一时间' in prompt
    assert 'location / time_cue / key_action 必须互不重复' not in prompt


def test_prior_scene_context_keeps_early_facts_without_promoting_planned_results():
    scene = SceneBrief(idx=1, title='交接', brief='计划', key_action='已经封存台账',
                       transition_bridge='站长已经签字')
    prose = '预约是在一个月前挂的。' + '车站里有候车的人。' * 100 + '陈川尚未接过钥匙。'
    context = SceneOrchestrator._summarize_scene(scene, prose)
    assert prose in context
    assert '预约是在一个月前挂的' in context
    assert '已经封存台账' not in context
    assert '站长已经签字' not in context
    assert SceneOrchestrator._summarize_scene(scene, '') == ''
