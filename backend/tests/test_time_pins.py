"""时间推进钉子:把大纲已声明的"次日清晨 / 一周后"钉进写手输入。

背景(2026-11-17 实测,100 章长跑):8 章被判 `time_progress_missing` ——
大纲声明了相对时间推进,正文通篇没有"过了多久"的说法。场景合同的 `time_delta`
在兜底简报里退化成占位话术("按本章大纲推导…"),正常路径也全靠写手自觉。

这里锁死三件事:
1. 只认"已经写明的说法",占位话术不算声明(否则每章都会要求交代时间);
2. 有相对推进时要求"让读者看出过了多久",只有时段时只要求写出时段;
3. 钉子真的出现在写手 user content 里(不是写在没人读的字段里)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import scene_orchestrator as so
from app.services.scene_orchestrator import _declared_time_cue
from app.services.time_pins import (
    declares_progress,
    extract_time_cue,
    time_pin_block,
    time_pin_lines,
)


# --- 抽取 -----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("次日清晨,周芸天没亮就去了店里。", "次日清晨"),
    ("一周后,铺租的事有了眉目。", "一周后"),
    ("十天后傍晚,老陈又来了。", "十天后傍晚"),
    ("过了三天,雨终于停了。", "过了三天"),
    ("隔了一夜,锅里的汤已经凉透。", "隔了一夜"),
])
def test_extracts_declared_progress(text, expected):
    assert extract_time_cue(text) == expected


def test_returns_empty_when_nothing_declared():
    assert extract_time_cue("周芸在店里算账,客人不多。") == ""
    assert extract_time_cue("", None, 123) == ""


def test_placeholder_from_fallback_brief_is_not_a_declaration():
    """兜底简报的占位话术不是时间声明,必须被忽略。"""
    placeholder = "按本章大纲推导,不得跳过关键时间成本"
    assert extract_time_cue(placeholder) == ""
    assert time_pin_block(placeholder) == ""
    assert declares_progress(placeholder) is False


def test_day_part_only_is_kept_but_not_treated_as_progress():
    assert extract_time_cue("本章承接当天傍晚,客人刚散。") == "当天"
    assert declares_progress("当天") is False


def test_first_source_wins_and_source_order_is_respected():
    assert extract_time_cue("次日清晨", "一周后") == "次日清晨"


def test_cue_length_is_capped():
    cue = extract_time_cue("一二三四五六七八九十天之后又三天之后")
    assert cue and len(cue) <= 16


# --- 渲染 -----------------------------------------------------------------

def test_block_requires_progress_to_be_visible():
    block = time_pin_block("次日清晨")
    assert "时间推进:次日清晨" in block
    assert "过了多久" in block
    assert "不要另起一段解释时间" in block


def test_block_for_day_part_only_does_not_demand_elapsed_time():
    block = time_pin_block("当天")
    assert "时间推进:当天" in block
    assert "过了多久" not in block
    assert "时段" in block


def test_block_is_empty_without_declaration():
    assert time_pin_block("") == ""
    assert time_pin_lines("") == []


def test_line_limit_is_honoured():
    assert len(time_pin_lines("次日清晨", limit=1)) == 1
    assert len(time_pin_lines("次日清晨", limit=99)) == 2


# --- orchestrator 取值 ----------------------------------------------------

def _pack(outline=None):
    return SimpleNamespace(
        current_outline=outline,
        character_cards=[],
        to_system_prompt=lambda: "SYSTEM",
        numeric_pin_lines=lambda: "",
    )


def _scene(**kwargs):
    base = dict(idx=1, target_words=600, title="场景 1", brief="",
                time_cue="", time_delta="", to_writer_user_content=lambda: "SCENE")
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_declared_cue_prefers_scene_brief():
    cue = _declared_time_cue(_scene(time_cue="次日清晨"), _pack())
    assert cue == "次日清晨"


def test_declared_cue_ignores_generic_time_delta_and_reads_outline():
    cue = _declared_time_cue(
        _scene(time_delta="按本章大纲推导,不得跳过关键时间成本"),
        _pack({"梗概": "老陈一周后又来收租。"}),
    )
    assert cue == "一周后"


def test_declared_cue_accepts_string_outline():
    assert _declared_time_cue(_scene(), _pack("第二天一早,周芸开了店。")) == "第二天一早"


def test_declared_cue_none_when_nothing_declared():
    assert _declared_time_cue(_scene(), _pack({"梗概": "周芸算账。"})) == ""


# --- 真的进了写手输入 -----------------------------------------------------

@pytest.mark.asyncio
async def test_pin_block_reaches_writer_user_content(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_stream_text_prompt(**kwargs):
        captured["user_content"] = kwargs["user_content"]
        yield "她把门板一块块卸下来。"

    monkeypatch.setattr(so, "stream_text_prompt", fake_stream_text_prompt)
    orch = so.SceneOrchestrator()
    chunks = []
    async for chunk in orch.write_scene_stream(
        scene=_scene(time_cue="次日清晨"),
        pack=_pack(),
        prior_scenes_summary="",
        db=None,
        project_id="p",
        chapter_id="c",
    ):
        chunks.append(chunk)

    assert "【本章时间推进(大纲已声明,正文必须体现)】" in captured["user_content"]
    assert "次日清晨" in captured["user_content"]
    assert "".join(chunks) == "她把门板一块块卸下来。"


@pytest.mark.asyncio
async def test_no_pin_block_when_chapter_declares_nothing(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_stream_text_prompt(**kwargs):
        captured["user_content"] = kwargs["user_content"]
        yield "正文。"

    monkeypatch.setattr(so, "stream_text_prompt", fake_stream_text_prompt)
    orch = so.SceneOrchestrator()
    async for _ in orch.write_scene_stream(
        scene=_scene(time_delta="按本章大纲推导,不得跳过关键时间成本"),
        pack=_pack({"梗概": "周芸算账。"}),
        prior_scenes_summary="",
        db=None,
        project_id="p",
        chapter_id="c",
    ):
        pass

    assert "本章时间推进" not in captured["user_content"]