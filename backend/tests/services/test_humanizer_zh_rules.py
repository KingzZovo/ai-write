"""Tests for the Humanizer-zh structural anti-AI rule module.

Covers: catalog shape, deterministic structural detection (negative
parallelism, shallow significance, false range, copula avoidance, over
qualification), prompt-block rendering + budget, the orthogonality
contract with the QMAI blacklist, and end-to-end wiring through
render_prose_quality_prompt + AntiAIChecker.
"""
from __future__ import annotations

import pytest


def test_catalog_covers_humanizer_structural_rules() -> None:
    from app.services.prompts.humanizer_zh_rules import HUMANIZER_RULES

    rule_ids = {rule.rule_id for rule in HUMANIZER_RULES}
    for must_have in (
        "negative_parallelism",
        "shallow_significance",
        "false_range",
        "copula_avoidance",
        "over_qualification",
        "synonym_cycling",
        "rule_of_three",
    ):
        assert must_have in rule_ids, must_have

    # Every rule must carry both directions of example and a prompt line.
    for rule in HUMANIZER_RULES:
        assert rule.bad_examples and rule.good_examples
        assert rule.prompt_instruction.strip()


def test_detects_negative_parallelism() -> None:
    from app.services.prompts.humanizer_zh_rules import scan_humanizer_structural

    text = (
        "这不是结束，而是另一个开始。"
        "他要的不仅仅是活下去，更是活成别人不敢想的样子。"
        "与其说他在逃跑，不如说他在重新选择战场。"
    )
    findings = {f["rule_id"]: f for f in scan_humanizer_structural(text)}
    assert "negative_parallelism" in findings
    assert findings["negative_parallelism"]["hits"] >= 3
    assert findings["negative_parallelism"]["severity"] == "medium"


def test_detects_shallow_significance() -> None:
    from app.services.prompts.humanizer_zh_rules import scan_humanizer_structural

    text = (
        "那道银光在他臂上游走，象征着某种古老力量的苏醒。"
        "断裂的门轴掉在地上，预示着这栋楼再没有退路。"
        "他攥紧了工牌，彰显出一种不肯认输的执拗。"
    )
    findings = {f["rule_id"]: f for f in scan_humanizer_structural(text)}
    assert "shallow_significance" in findings
    assert findings["shallow_significance"]["hits"] >= 3


def test_detects_copula_avoidance_and_false_range() -> None:
    from app.services.prompts.humanizer_zh_rules import scan_humanizer_structural

    text = (
        "他作为这条街上唯一的外来者的存在，格外扎眼。"
        "那把锈刀堪称他全部的家当。"
        "从街角的早餐摊到城市尽头的写字楼，从清晨的露水到深夜的霓虹，这座城什么都有。"
    )
    findings = {f["rule_id"]: f for f in scan_humanizer_structural(text)}
    assert "copula_avoidance" in findings
    assert "false_range" in findings


def test_detects_stacked_over_qualification() -> None:
    from app.services.prompts.humanizer_zh_rules import scan_humanizer_structural

    text = "他似乎大概觉得，这里也许可能不太安全。"
    findings = {f["rule_id"]: f for f in scan_humanizer_structural(text)}
    assert "over_qualification" in findings


def test_clean_prose_yields_no_structural_findings() -> None:
    from app.services.prompts.humanizer_zh_rules import scan_humanizer_structural

    # Plain, concrete, human prose — none of the structural scaffolds.
    text = (
        "林照推开门，看见走廊尽头蹲着一具骨架。他后退一步，门轴吱呀响了一声。"
        "楼道里没有灯，他摸着墙往下走，鞋底踩过碎玻璃。"
        "手机只剩百分之五，他把它揣回兜里，没再看。"
    )
    assert scan_humanizer_structural(text) == []


def test_prompt_block_renders_and_respects_budget() -> None:
    from app.services.prompts.humanizer_zh_rules import render_humanizer_prompt_block

    block = render_humanizer_prompt_block()
    assert "humanizer_zh" in block
    assert "negative_parallelism" in block
    assert 0 < len(block) <= 1400

    tight = render_humanizer_prompt_block(max_chars=200)
    assert 0 < len(tight) <= 200
    # Highest-priority rule survives the tightest budget.
    assert "humanizer_zh" in tight


def test_render_prose_quality_prompt_includes_humanizer_block() -> None:
    from app.services.prose_quality_rules import render_prose_quality_prompt

    prompt = render_prose_quality_prompt()
    # Both orthogonal blocks must be present in the single injection point.
    assert "anti_ai_phrase_blacklist" in prompt  # QMAI lexical
    assert "humanizer_zh" in prompt  # Humanizer structural
    assert "negative_parallelism" in prompt
    assert "synonym_cycling" in prompt


def test_blueprint_carries_humanizer_block_exactly_once() -> None:
    from app.services.narrative_quality_gates import preflight_scene_blueprint_prompt

    blueprint = preflight_scene_blueprint_prompt(chapter_idx=1)
    assert blueprint.count("humanizer_zh｜") == 1


@pytest.mark.asyncio
async def test_checker_emits_structural_issue() -> None:
    from app.services.checkers.anti_ai_checker import AntiAIChecker
    from app.services.context_pack import ContextPack

    filler = "他沿着街往前走，路灯一盏一盏亮起来，巷口的烤肠摊还没收摊。"
    text = (
        "这不是结束，而是另一个开始。" + filler
        + "他要的不仅仅是活下去，更是活成别人不敢想的样子。" + filler
        + "与其说他在逃跑，不如说他在重新选择战场。" + filler
    )
    assert len(text) >= 100

    checker = AntiAIChecker()
    result = await checker.check(text, ContextPack())
    structural = [
        i for i in result.issues if str(i.get("type", "")).startswith("humanizer_")
    ]
    assert structural, f"expected humanizer_* issue, got: {result.issues}"
    assert any("否定式排比" in i["description"] for i in structural)


@pytest.mark.asyncio
async def test_checker_clean_text_no_structural_issue() -> None:
    from app.services.checkers.anti_ai_checker import AntiAIChecker
    from app.services.context_pack import ContextPack

    text = (
        "林照推开门，看见走廊尽头蹲着一具骨架。他后退一步，门轴吱呀响了一声。"
        "楼道里没有灯，他摸着墙往下走，鞋底踩过碎玻璃，手机只剩百分之五。"
        "他把手机揣回兜里，贴着墙根继续往下，没有回头。"
    )
    checker = AntiAIChecker()
    result = await checker.check(text, ContextPack())
    structural = [
        i for i in result.issues if str(i.get("type", "")).startswith("humanizer_")
    ]
    assert not structural, f"clean text tripped: {structural}"
