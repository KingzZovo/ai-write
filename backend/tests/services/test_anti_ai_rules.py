"""Tests for the QMAI-derived anti-AI prose rule catalog.

Covers: blacklist size/renderability, checker wiring (merged detection
source), prompt injection into render_prose_quality_prompt, and the
render budget contract.
"""
from __future__ import annotations

import pytest


def test_blacklist_nonempty_and_renderable():
    from app.services.prompts.anti_ai_rules_zh import (
        AI_PHRASE_BLACKLIST,
        ANTI_OVERCORRECTION,
        DIALOGUE_PRINCIPLES,
        EMOTION_TO_ACTION_EXAMPLES,
        render_anti_ai_prompt_block,
    )

    total = sum(len(v) for v in AI_PHRASE_BLACKLIST.values())
    assert total >= 40

    # Task-mandated signature entries must be present somewhere in the catalog.
    flat = [p for phrases in AI_PHRASE_BLACKLIST.values() for p in phrases]
    for must_have in (
        "微微一愣",
        "眼中闪过一丝",
        "命运的齿轮开始转动",
        "空气突然安静下来",
    ):
        assert must_have in flat, must_have

    # No duplicate entries across categories.
    assert len(flat) == len(set(flat))

    assert 4 <= len(EMOTION_TO_ACTION_EXAMPLES) <= 6
    for example in EMOTION_TO_ACTION_EXAMPLES:
        assert example["bad"].strip()
        assert example["good"].strip()
    assert DIALOGUE_PRINCIPLES
    assert ANTI_OVERCORRECTION

    block = render_anti_ai_prompt_block()
    assert 0 < len(block) <= 1800


@pytest.mark.asyncio
async def test_checker_detects_new_blacklist_phrases():
    from app.services.checkers.anti_ai_checker import AI_PHRASES, AntiAIChecker
    from app.services.context_pack import ContextPack

    # The QMAI blacklist must be merged into the phrase detection source.
    assert "命运的齿轮开始转动" in AI_PHRASES
    assert "空气突然安静下来" in AI_PHRASES
    # Pre-existing entries must survive the merge untouched.
    assert "映入眼帘" in AI_PHRASES
    assert "眼中闪过一丝" in AI_PHRASES
    # The merge must not introduce duplicates.
    assert len(AI_PHRASES) == len(set(AI_PHRASES))

    filler = "他沿着街走，路灯一盏一盏亮起来，巷口的烤肠摊还没收，老板正把签子一根根插好。"
    text = (
        "命运的齿轮开始转动。" + filler
        + "空气突然安静下来。" + filler
        + "他微微一愣，又轻轻点头。" + filler
        + "她嘴角勾起一抹笑。" + filler
    )
    assert len(text) >= 100

    checker = AntiAIChecker()
    result = await checker.check(text, ContextPack())
    phrase_issues = [i for i in result.issues if i.get("type") == "ai_phrases"]
    assert phrase_issues, f"expected ai_phrases issue, got: {result.issues}"
    description = phrase_issues[0]["description"]
    assert "命运的齿轮开始转动" in description


def test_prose_quality_prompt_includes_anti_ai_block():
    from app.services.prose_quality_rules import render_prose_quality_prompt

    prompt = render_prose_quality_prompt()
    assert "anti_ai_phrase_blacklist" in prompt
    assert "命运的齿轮开始转动" in prompt
    # Anti-overcorrection guards must reach the prompt surface too.
    assert "禁止把爽文改得过于文艺" in prompt


def test_blueprint_carries_anti_ai_block_once():
    """The blueprint consumes render_prose_quality_prompt once; the QMAI
    block must therefore appear exactly once (no duplicate injection)."""
    from app.services.narrative_quality_gates import preflight_scene_blueprint_prompt

    blueprint = preflight_scene_blueprint_prompt(chapter_idx=1)
    assert blueprint.count("anti_ai_phrase_blacklist") == 1


def test_render_respects_budget():
    from app.services.prompts.anti_ai_rules_zh import render_anti_ai_prompt_block

    block = render_anti_ai_prompt_block(max_chars=300)
    assert 0 < len(block) <= 300

    default_block = render_anti_ai_prompt_block()
    assert len(default_block) <= 1800
    # Blacklist entries get priority under tight budgets.
    assert "陈词滥调" in block or "微微一愣" in block
