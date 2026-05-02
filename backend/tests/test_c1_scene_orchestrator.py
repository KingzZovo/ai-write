"""v1.5.0 C1 — SceneOrchestrator regression suite.

Covers:
  1. SceneBrief.from_dict clamps target_words into [800, 1200] and falls
     back on empty/garbage inputs.
  2. _try_parse_scene_array handles strict JSON, fenced ```json blocks,
     wrapped {"scenes": [...]} dict shape, and returns None for garbage.
  3. _fallback_scene_briefs always yields 3..6 briefs with target_words in
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
    _try_parse_scene_array,
)


class _FakePack:
    """Minimal stand-in for ContextPack used in scene tests."""

    def __init__(self, system_prompt: str = "<world rules>\n<chapter outline body>") -> None:
        self._system_prompt = system_prompt

    def to_system_prompt(self) -> str:
        return self._system_prompt

    def to_messages(self, user_instruction: str = "") -> list[dict]:
        msgs = [{"role": "system", "content": self._system_prompt}]
        msgs.append({"role": "user", "content": user_instruction or "生成"})
        return msgs


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


async def _async_iter(chunks):
    for c in chunks:
        yield c


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


# 3) ---------- _fallback_scene_briefs ----------

@pytest.mark.parametrize(
    "target,expected_min_n,expected_max_n",
    [
        (3000, 3, 3),
        (3500, 3, 4),
        (5000, 4, 5),
        (6500, 6, 6),
        (10000, 6, 6),  # capped at 6
    ],
)
def test_fallback_scene_count_scales(target, expected_min_n, expected_max_n):
    out = _fallback_scene_briefs(target, "A" * 800)
    assert expected_min_n <= len(out) <= expected_max_n, (target, len(out))
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


def test_fallback_last_scene_hook_empty():
    out = _fallback_scene_briefs(4000, "x" * 500)
    assert out[-1].hook == ""
    assert all(b.hook for b in out[:-1])


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


# 5) ---------- SceneOrchestrator.plan_scenes happy path ----------

@pytest.mark.asyncio
async def test_plan_scenes_uses_llm_when_json_parses():
    pack = _FakePack()
    fake_briefs = [
        {"title": "起", "brief": "起头", "pov": "A", "target_words": 900, "hook": "h1"},
        {"title": "承", "brief": "转折", "pov": "A", "target_words": 1100, "hook": "h2"},
        {"title": "转", "brief": "高潮", "pov": "A", "target_words": 1100, "hook": "h3"},
        {"title": "合", "brief": "收尾", "pov": "A", "target_words": 900, "hook": ""},
    ]
    fake_text = json.dumps(fake_briefs, ensure_ascii=False)
    with patch(
        "app.services.scene_orchestrator.run_text_prompt",
        new=AsyncMock(return_value=_FakeResult(fake_text)),
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


# 6) ---------- plan_scenes falls back on garbage ----------

@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_unparseable():
    pack = _FakePack()
    with patch(
        "app.services.scene_orchestrator.run_text_prompt",
        new=AsyncMock(return_value=_FakeResult("sorry I cannot do this")),
    ):
        orch = SceneOrchestrator()
        out = await orch.plan_scenes(
            pack=pack, db=None, project_id="p", chapter_id=None,
            target_words=3500, n_scenes_hint=None,
        )
    assert 3 <= len(out) <= 6
    assert all(MIN_SCENE_WORDS <= b.target_words <= MAX_SCENE_WORDS for b in out)


# 7) ---------- plan_scenes falls back on LLM exception ----------

@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_llm_exception():
    pack = _FakePack()
    with patch(
        "app.services.scene_orchestrator.run_text_prompt",
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
        {"title": "a", "brief": "x", "target_words": 900, "hook": "h"},
        {"title": "b", "brief": "y", "target_words": 900, "hook": "h"},
        {"title": "c", "brief": "z", "target_words": 900, "hook": ""},
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
        "app.services.scene_orchestrator.run_text_prompt",
        new=AsyncMock(return_value=_FakeResult(briefs_json)),
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
        {"title": f"场 {i}", "brief": "b", "target_words": 900, "hook": "h" if i < 3 else ""}
        for i in (1, 2, 3)
    ], ensure_ascii=False)
    seen: list[int] = []

    async def cb(scene):
        seen.append(scene.idx)

    def _stream(*args, **kwargs):
        return _async_iter(["x"])

    with patch(
        "app.services.scene_orchestrator.run_text_prompt",
        new=AsyncMock(return_value=_FakeResult(briefs_json)),
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
