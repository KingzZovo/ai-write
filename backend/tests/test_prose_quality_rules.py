from app.services.prose_quality_rules import (
    PROSE_QUALITY_RULES,
    metric_names,
    regex_patterns_for,
    render_prose_quality_prompt,
)


def test_rule_catalog_contains_cross_project_lessons() -> None:
    rule_ids = {rule.rule_id for rule in PROSE_QUALITY_RULES}

    assert "plain_contemporary_chinese" in rule_ids
    assert "semantic_collocation_completeness" in rule_ids
    assert "resource_and_scene_logic" in rule_ids
    assert "duplicate_explanation_control" in rule_ids
    assert "dialogue_human_friction" in rule_ids


def test_rule_catalog_exposes_metrics_without_project_names() -> None:
    names = metric_names()

    assert "pseudo_literary_register_count" in names
    assert "plain_contemporary_violation_count" in names
    assert "duplicate_explanation_span_count" in names
    joined = "\n".join(rule.prompt_instruction for rule in PROSE_QUALITY_RULES)
    assert "神裔" not in joined
    assert "雨夜借宿" not in joined


def test_plain_contemporary_patterns_are_generalized() -> None:
    patterns = regex_patterns_for("plain_contemporary_chinese")
    joined = "\n".join(patterns)

    assert "喊了声" in joined
    assert "来意" in joined
    assert "说得很低" in joined
    assert "声音被" in joined


def test_rendered_prompt_contains_bad_and_good_examples() -> None:
    prompt = render_prose_quality_prompt()

    assert "plain_contemporary_chinese" in prompt
    assert "伪文学压缩腔" in prompt
    assert "喊了声师傅" in prompt
    assert "他叫了一声" in prompt
    assert "先归因到规则族" in prompt
