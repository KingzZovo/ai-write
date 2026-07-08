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
    ):
        assert vtype in EVALUATION_SYSTEM_PROMPT, f"missing taxonomy tag: {vtype}"


def test_evaluation_max_tokens_not_truncating():
    import inspect

    from app.services import chapter_evaluator

    src = inspect.getsource(chapter_evaluator)
    assert "max_tokens=900" not in src
    assert "max_tokens=2400" in src


def test_system_prompt_contains_calibration():
    """C1 (ainovel editor doctrine): the over-revision calibration block must be
    spliced into the evaluator system prompt so the verdict regresses toward
    'accept is the common result' rather than systematic score-lowering."""
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT

    for marker in (
        "评审校准",          # block header
        "accept",            # "accept is the most common, correct result"
        "最常见",
        "对话区分度",        # dialogue-distinctness test
        "1-2 处",            # "must point at the 1-2 spots to strengthen"
        "quote",             # every issue needs verbatim evidence
    ):
        assert marker in EVALUATION_SYSTEM_PROMPT, f"missing calibration marker: {marker}"
