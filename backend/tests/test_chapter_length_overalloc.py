"""章级篇幅:超分配(送模目标 ×1.08)+ 章级验收下限 0.95(2026-11-18)。

背景(实测,不是推测):
  gemini-3.1-pro @ 目标 2000 字、100 章长跑,`continuity.json` 的 word_counts:
  中位 **1952 字(比 0.976)**、均值 1944;92 章里 55 章 <2000、41 章 <1900。
  (去掉空白字符后中位 1854 —— 中文"字数"习惯不含空白,两个口径都记着。)
  即每场系统性欠 2~3%,而场级续写阈值只对"明显不够"的场触发一次,补不回这点零头。

处置:
  1. `SCENE_TARGET_OVERALLOC=1.08` —— 只放大**送给规划器/写手的目标**,让模型按
     2160 写、欠完 ≈2106;章节记录里的真实目标、验收、统计口径都不变。
  2. `CHAPTER_MIN_TARGET_WORD_RATIO` 0.80 → 0.95 —— 0.80(=1600)会把 1952 字这种
     接近目标但不足的章直接标 passed;0.95(=1900)允许 5% 浮动。

本文件只做离线断言,不调用任何真实模型。
"""

from __future__ import annotations

import pytest

from app.services.scene_orchestrator import _planning_target


# --- 超分配系数 -------------------------------------------------------------

def test_planning_target_default_is_eight_percent_more():
    """实测欠幅 ~2.4%,放大 8% → 预期中位 2106,大多数章落在 2000 以上。"""
    assert _planning_target(2000) == 2160
    assert _planning_target(1200) == 1296


def test_planning_target_is_one_when_disabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", 1.0, raising=False)
    assert _planning_target(2000) == 2000


def test_planning_target_honours_configured_factor(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", 1.4, raising=False)
    assert _planning_target(2000) == 2800


def test_planning_target_clamps_and_tolerates_garbage(monkeypatch):
    from app.config import settings

    # 上界 1.5:防止误配把一章的预算吹到 2 倍以上。
    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", 9.0, raising=False)
    assert _planning_target(2000) == 3000
    # 小于 1 一律当 1(不允许把送模目标调低)。
    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", 0.5, raising=False)
    assert _planning_target(2000) == 2000
    # 非数字/None 回退到关闭状态,不抛。
    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", "abc", raising=False)
    assert _planning_target(2000) == 2000
    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", None, raising=False)
    assert _planning_target(2000) == 2000


def test_planning_target_keeps_zero_and_negative_untouched(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCENE_TARGET_OVERALLOC", 1.1, raising=False)
    assert _planning_target(0) == 0
    assert _planning_target(None) == 0
    assert _planning_target(-5) == 0


def test_planning_target_does_not_break_planner_budget_window(monkeypatch):
    """放大后的目标仍要能算出合法的 85%–115% 预算窗(规划器校验用它)。"""
    from app.services.scene_plan_contract import scene_budget_bounds

    enlarged = _planning_target(2000)
    lower, upper = scene_budget_bounds(enlarged)
    assert lower < enlarged < upper
    assert enlarged * 0.85 <= lower <= enlarged * 0.86
    assert enlarged * 1.14 <= upper <= enlarged * 1.15


# --- 章级验收下限 -----------------------------------------------------------

def test_chapter_min_target_ratio_default_is_one():
    """2026-11-28:作者的硬规则是"低于 2000 字都不能发布",所以验收下限不再留 5% 浮动。"""
    from app.services.chapter_quality_gate import (
        _MIN_TARGET_WORD_RATIO,
        _resolve_min_target_ratio,
    )

    assert _resolve_min_target_ratio() == pytest.approx(1.0)
    assert _MIN_TARGET_WORD_RATIO == pytest.approx(1.0)


def test_chapter_min_target_count_for_2000_is_2000():
    from app.services.chapter_quality_gate import _min_target_word_count

    assert _min_target_word_count(2000) == 2000
    assert _min_target_word_count(None) is None
    assert _min_target_word_count(0) is None


def test_shortfall_boundary_at_2000_chars():
    from app.services.chapter_quality_gate import _target_length_shortfall

    # 1900 曾经算"达标"(0.95 浮动),按新规则必须算偏短。
    assert _target_length_shortfall("字" * 1900, target_word_count=2000) is True
    assert _target_length_shortfall("字" * 1999, target_word_count=2000) is True
    assert _target_length_shortfall("字" * 2000, target_word_count=2000) is False
    # 没有目标字数时不做长度判定。
    assert _target_length_shortfall("字" * 10, target_word_count=None) is False


def test_ratio_is_read_from_config_at_call_time(monkeypatch):
    """口径只有一处来源:改配置项即可,不需要动调用点。"""
    from app.config import settings
    from app.services.chapter_quality_gate import _min_target_word_count

    monkeypatch.setattr(settings, "CHAPTER_MIN_TARGET_WORD_RATIO", 0.80, raising=False)
    assert _min_target_word_count(2000, min_target_word_ratio=0.80) == 1600
    monkeypatch.setattr(settings, "CHAPTER_MIN_TARGET_WORD_RATIO", 0.95, raising=False)
    assert _min_target_word_count(2000, min_target_word_ratio=0.95) == 1900
