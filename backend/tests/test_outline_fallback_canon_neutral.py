"""Regression: staged book-outline fallbacks must stay canon-neutral.

When the relay is slow (>OUTLINE_FAST_CALL_TIMEOUT_SECONDS) the staged
book-outline generator falls back to deterministic local text. Those
fallbacks were hardcoded with one stale 神裔 conception (沈砚 / 血印 / 血税 /
王朝档案司 / 祭台) that CONTRADICTS the project's own canonical-facts lock
(protagonist 林照, 回声塌陷, 暗面学院, 活体滤波接口, 清除署). The result was a
book outline whose skeleton (from the LLM, on-canon) and characters/world
sections (from the fallback, off-canon) named two different protagonists and
two different worlds.

A generic outline generator must not fabricate book-specific names. The
fallbacks must echo the caller's ``user_input`` (which carries the
authoritative facts) and must NOT inject a hardcoded protagonist/world.
"""
from __future__ import annotations

from app.services.outline_generator import OutlineGenerator

# A user_input carrying the 神裔 canonical facts (condensed).
SHENYI_INPUT = (
    "书名：神裔\n类型：都市奇幻\n"
    "创意/前提：18岁少年林照在老小区异常‘回声塌陷’中误入血脉世界，"
    "被女性高阶血脉者沈听澜救下，进入暗面学院，逆向追查父母失踪、"
    "活体滤波接口与被抹除旧神‘无名神核’的真相。"
)

# Hardcoded names from the OLD stale fallback that must never be injected.
BANNED_FALLBACK_TOKENS = ["沈砚", "陆青棠", "纪无尘", "严伯川", "太祝玄衡", "王朝档案司", "血税", "血印第"]


def test_book_skeleton_fallback_echoes_user_input_not_hardcoded_canon():
    text = OutlineGenerator._fallback_book_skeleton(SHENYI_INPUT, scale={"n_volumes": 5, "chapters_per_volume": 150, "n_chapters": 750})
    # Must carry the caller's facts forward...
    assert "林照" in text or SHENYI_INPUT[:20] in text
    # ...and must NOT inject the stale off-canon protagonist/world.
    leaked = [t for t in BANNED_FALLBACK_TOKENS if t in text]
    assert not leaked, f"book skeleton fallback leaked off-canon tokens: {leaked}"


import pytest


@pytest.mark.parametrize("stage", ["B2/characters", "B4/relationships", "B5/factions", "C/world"])
def test_small_stage_fallback_echoes_user_input_not_hardcoded_canon(stage):
    text = OutlineGenerator._fallback_small_stage_text(stage, SHENYI_INPUT)
    # Each scaffold must defer to the project's facts (echo the canonical input)...
    assert "林照" in text, f"{stage} fallback dropped the project's protagonist"
    # ...and must NOT inject the stale off-canon names/world.
    leaked = [t for t in BANNED_FALLBACK_TOKENS if t in text]
    assert not leaked, f"{stage} fallback leaked off-canon tokens: {leaked}"

