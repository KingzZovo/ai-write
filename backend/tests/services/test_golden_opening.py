"""Tests for golden-three-chapters opening constraints (Q4, adapted from QMAI).

The chapter blueprint must inject hard opening rules for chapters 1-3 only.
"""
from __future__ import annotations

from app.services.narrative_quality_gates import (
    GOLDEN_OPENING_RULES,
    preflight_scene_blueprint_prompt,
)

MARKER = "开篇硬约束"


def test_chapter_two_blueprint_contains_golden_rules():
    blueprint = preflight_scene_blueprint_prompt(chapter_idx=2)
    assert MARKER in blueprint
    assert GOLDEN_OPENING_RULES.strip() in blueprint


def test_chapter_three_boundary_included():
    assert MARKER in preflight_scene_blueprint_prompt(chapter_idx=3)


def test_chapter_four_and_beyond_excluded():
    assert MARKER not in preflight_scene_blueprint_prompt(chapter_idx=4)
    assert MARKER not in preflight_scene_blueprint_prompt(chapter_idx=5)


def test_chapter_idx_none_excluded():
    assert MARKER not in preflight_scene_blueprint_prompt(chapter_idx=None)
    assert MARKER not in preflight_scene_blueprint_prompt()


def test_golden_rules_content():
    text = GOLDEN_OPENING_RULES
    assert "300-500" in text
    assert "禁止成段解释" in text
    assert "钩子" in text
    # Guard: do not introduce「N、」section ordinals that would collide with
    # the blueprint's numbered-section uniqueness check.
    import re

    assert not re.search(r"^[零一二三四五六七八九十]+、", text, re.MULTILINE)
