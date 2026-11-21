"""Quality-debt terminal semantics (offline: no DB, no model, no session).

Pins the four structured terminal actions, the ``ChapterAcceptance``
``terminal_action`` / ``persisted_status`` integration, and the invariant that
an empty body is never promoted to ``completed`` by quality debt.
"""

from __future__ import annotations

from app.services.chapter_acceptance import ChapterAcceptance
from app.services.chapter_quality_gate import classify_issue_severity
from app.services.quality_debt import (
    CONTINUE_WITH_WARNING,
    DEFER_AND_CONTINUE,
    LOCAL_PATCH_PLAN,
    STOP_FOR_REPLAN,
    TERMINAL_ACTIONS,
    classify_terminal_action,
)

PASSING = {"overall": 9.0, "issues": []}
# Ordinary feedback: the aesthetic expression tag is non-blocking even at high
# severity (pinned by tests/test_chapter_acceptance.py), and the score is low.
ORDINARY = {
    "overall": 7.4,
    "issues": [{"violation_type": "expression_contract_violation", "severity": "high"}],
}
LOCATED = {
    "overall": 9.6,
    "issues": [
        {
            "violation_type": "space_rule_violation",
            "severity": "low",
            "description": "人物位置与动作承接不一致。",
            "quote": "他还站在屋里。",
            "quote_verified": True,
        }
    ],
}


def _classify(**overrides):
    base = dict(
        mechanical_status="completed",
        evaluation=PASSING,
        located_causal_issue=False,
        missing_obligations=[],
        plan_misaligned=False,
    )
    base.update(overrides)
    return classify_terminal_action(**base)


def test_actions_are_the_four_declared_values() -> None:
    assert set(TERMINAL_ACTIONS) == {
        CONTINUE_WITH_WARNING,
        LOCAL_PATCH_PLAN,
        DEFER_AND_CONTINUE,
        STOP_FOR_REPLAN,
    }


def test_clean_chapter_continues_with_warning_without_reasons() -> None:
    result = _classify()
    assert result["action"] == CONTINUE_WITH_WARNING
    assert result["debt"] is False
    assert result["reasons"] == []
    assert result["blocking"] == []


def test_located_causal_issue_is_a_local_patch_not_a_stop() -> None:
    result = _classify(evaluation=LOCATED, located_causal_issue=True)
    assert result["action"] == LOCAL_PATCH_PLAN
    assert result["debt"] is False
    assert "located_causal_issue" in result["reasons"]


def test_ordinary_quality_problem_becomes_quality_debt() -> None:
    result = _classify(evaluation=ORDINARY)
    assert result["action"] == DEFER_AND_CONTINUE
    assert result["debt"] is True
    assert result["reasons"]
    assert result["blocking"] == []


def test_plan_misalignment_stops_for_replan() -> None:
    result = _classify(plan_misaligned=True)
    assert result["action"] == STOP_FOR_REPLAN
    assert result["debt"] is False
    assert "plan_misaligned" in result["blocking"]


def test_missing_obligation_stops_for_replan() -> None:
    result = _classify(missing_obligations=["交付账本"])
    assert result["action"] == STOP_FOR_REPLAN
    assert "missing_obligation:交付账本" in result["blocking"]


def test_mechanical_failure_stops_even_without_an_evaluation() -> None:
    result = _classify(mechanical_status="draft", evaluation=None)
    assert result["action"] == STOP_FOR_REPLAN
    assert any(token.startswith("mechanical:") for token in result["blocking"])


def test_plan_misalignment_outranks_a_located_causal_issue() -> None:
    result = _classify(
        located_causal_issue=True, evaluation=LOCATED, plan_misaligned=True
    )
    assert result["action"] == STOP_FOR_REPLAN


def test_severity_split_separates_causal_from_aesthetic() -> None:
    split = classify_issue_severity(LOCATED["issues"] + ORDINARY["issues"])
    assert [i["violation_type"] for i in split["blocking"]] == ["space_rule_violation"]
    assert [
        i["violation_type"] for i in split["non_blocking"]
    ] == ["expression_contract_violation"]
    assert classify_issue_severity(None) == {"blocking": [], "non_blocking": []}
    assert classify_issue_severity("not-a-list") == {"blocking": [], "non_blocking": []}


# --- ChapterAcceptance integration -----------------------------------------


def test_defer_and_continue_persists_completed_with_debt() -> None:
    acceptance = ChapterAcceptance(required=True)
    acceptance.record("正文。", ORDINARY)
    action = acceptance.terminal_action("正文。")
    assert action["action"] == DEFER_AND_CONTINUE
    assert action["debt"] is True
    assert acceptance.persisted_status("正文。") == "completed"


def test_stop_for_replan_persists_needs_review() -> None:
    acceptance = ChapterAcceptance(required=False)
    assert acceptance.status("正文。") == "completed"
    persisted = acceptance.persisted_status("正文。", {"plan_misaligned": True})
    assert persisted == "needs_review"


def test_clean_accepted_chapter_persists_completed() -> None:
    acceptance = ChapterAcceptance(required=True)
    acceptance.record("正文。", PASSING)
    assert acceptance.terminal_action("正文。")["action"] == CONTINUE_WITH_WARNING
    assert acceptance.persisted_status("正文。") == "completed"


def test_empty_body_is_never_completed_by_quality_debt() -> None:
    acceptance = ChapterAcceptance(required=True)
    assert acceptance.persisted_status("") == "draft"
    assert acceptance.persisted_status("   ") == "draft"
    assert acceptance.persisted_status("", {"plan_misaligned": True}) == "draft"


def test_existing_status_behaviour_is_unchanged() -> None:
    receipt = ChapterAcceptance(required=True)
    assert receipt.status("原稿。") == "needs_review"
    receipt.record("原稿。", {"overall": 8.8})
    assert receipt.status("原稿。") == "completed"
    assert receipt.status("润色后删掉关键事件。") == "needs_review"
    assert receipt.status("原稿。", {"persisted_on_block": True}) == "draft"
    assert ChapterAcceptance(required=False).status("原稿。") == "completed"
    assert ChapterAcceptance(required=True).status("原稿还没写") == "draft"
