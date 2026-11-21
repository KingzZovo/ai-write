"""Evaluator must know the violation taxonomy and have headroom for 5-dim JSON output."""


def test_system_prompt_contains_violation_taxonomy():
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT
    from app.services.narrative_contract import EVALUATOR_CONTRACT_PROMPT

    assert EVALUATOR_CONTRACT_PROMPT.strip()[:80] in EVALUATION_SYSTEM_PROMPT


def test_system_prompt_lists_all_violation_types():
    """Every [xxx_violation] tag downstream consumers depend on must be in the prompt."""
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT

    for vtype in (
        "time_rule_violation",
        "space_rule_violation",
        "power_resource_violation",
        "information_rule_violation",
        "mechanism_rule_violation",
        "result_strength_violation",
        "expression_contract_violation",
        # Q3 v1.9.1: character cognition ledger violation.
        "cognition_violation",
        "mundane_plausibility_violation",
    ):
        assert vtype in EVALUATION_SYSTEM_PROMPT, f"missing taxonomy tag: {vtype}"


# Output-budget coverage lives in test_chapter_evaluation_registry.py: it
# checks the configured budget at the actual invocation boundary, rather than
# requiring a hard-coded numeric literal in the evaluator's source.


def test_system_prompt_contains_calibration():
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT
    for marker in ("评审校准", "不预设 accept 应占多数", "逐字 quote", "不能以“合理取舍”豁免"):
        assert marker in EVALUATION_SYSTEM_PROMPT
    assert "最常见、最正确的结果" not in EVALUATION_SYSTEM_PROMPT
