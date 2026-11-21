"""Structured terminal semantics for chapter acceptance ("quality debt").

A long serial run cannot afford to stop on every chapter whose prose is usable
but not flawless. The existing acceptance verdict collapses all of those into
``needs_review``, which deadlocks generation (borrowed from the
AI-Novel-Writing-Assistant "quality debt" idea). This module separates the
*why* a chapter is not perfect into four terminal actions:

* ``continue_with_warning`` -- clean enough; note it and keep going (lightest).
* ``local_patch_plan``      -- a defect located inside THIS chapter; fix it in
                               place, do not touch the global plan.
* ``defer_and_continue``    -- ordinary quality debt: write forward and collect
                               it for later reaping (the key action for long
                               runs; ``debt=True``).
* ``stop_for_replan``       -- ONLY a plan mismatch / unmet blocking obligation
                               (or a mechanically unusable body) really stops.

``classify_terminal_action`` is a pure function (no IO, no model, no session),
so it is directly unit-testable.
"""
from __future__ import annotations

import math
from typing import Any

CONTINUE_WITH_WARNING = "continue_with_warning"
LOCAL_PATCH_PLAN = "local_patch_plan"
DEFER_AND_CONTINUE = "defer_and_continue"
STOP_FOR_REPLAN = "stop_for_replan"

TERMINAL_ACTIONS: tuple[str, ...] = (
    CONTINUE_WITH_WARNING,
    LOCAL_PATCH_PLAN,
    DEFER_AND_CONTINUE,
    STOP_FOR_REPLAN,
)

# Only deferral records debt; a local patch is a same-chapter fix, not debt.
_DEBT_ACTIONS = frozenset({DEFER_AND_CONTINUE})

# Statuses that mean the body itself is unusable (empty / truncated / refusal /
# relay error / scaffolding / persisted-on-block). A caller may pass either the
# mechanical status (``chapter_persist_status``) or the acceptance status
# (``ChapterAcceptance.status``); both surface an unusable body as ``draft``.
_MECHANICAL_FAILURE_STATUSES = frozenset({"draft", "blocked", "failed"})

# Mirrors ``chapter_acceptance.evaluation_passes`` default; kept local so the
# classifier stays a pure function with no import-time coupling to acceptance.
_DEFAULT_THRESHOLD = 8.2


def _field(result: Any, name: str, default=None):
    return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)


def _evaluation_passes(evaluation: Any) -> bool:
    from app.services.chapter_acceptance import evaluation_passes

    return evaluation_passes(evaluation)


def _issue_types(evaluation: Any) -> tuple[list[str], list[str]]:
    """(blocking_types, non_blocking_types) for the evaluation's issues.

    Purely a re-projection of the existing acceptance semantics via
    ``chapter_quality_gate.classify_issue_severity``. Returns empty lists when
    the evaluation carries no issues (or the helpers cannot be imported).
    """
    try:
        from app.services.chapter_quality_gate import classify_issue_severity
        from app.services.narrative_quality_gates import coerce_issues, issue_violation_type
    except Exception:  # noqa: BLE001 - classification must never raise
        return [], []

    split = classify_issue_severity(coerce_issues(evaluation))

    def _tags(items: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for issue in items:
            tag = issue_violation_type(issue)
            if tag and tag not in out:
                out.append(tag)
        return out

    return _tags(split["blocking"]), _tags(split["non_blocking"])


def _quality_reasons(evaluation: Any, non_blocking_types: list[str]) -> list[str]:
    reasons: list[str] = []
    if evaluation is None:
        reasons.append("evaluation_unavailable")
    elif _field(evaluation, "parse_failed", False):
        reasons.append("evaluation_parse_failed")
    else:
        score = _field(evaluation, "overall")
        if type(score) not in (int, float) or not math.isfinite(score):
            reasons.append("evaluation_unscored")
        elif score < _DEFAULT_THRESHOLD:
            reasons.append("evaluation_below_threshold")
    for tag in non_blocking_types:
        reason = f"quality:{tag}"
        if reason not in reasons:
            reasons.append(reason)
    if not reasons:
        reasons.append("quality_issue")
    return reasons


def _result(action: str, reasons: list[str], blocking: list[str]) -> dict:
    return {
        "action": action,
        "debt": action in _DEBT_ACTIONS,
        "reasons": reasons,
        "blocking": blocking,
    }


def classify_terminal_action(
    *,
    mechanical_status: str,
    evaluation: Any,
    located_causal_issue: bool,
    missing_obligations: list[str],
    plan_misaligned: bool,
) -> dict:
    """Map mechanical + evaluation signals to one structured terminal action.

    Rule table (first match wins):

    1. ``plan_misaligned`` or any ``missing_obligations`` -> ``stop_for_replan``
       (blocking populated). This is the only true planner-level stop.
    2. a mechanically unusable body (``draft``/``blocked``/``failed``) ->
       ``stop_for_replan``; there is nothing usable to defer.
    3. a located causal issue, or any blocking (causal high/critical) issue ->
       ``local_patch_plan`` (fix THIS chapter, keep the plan).
    4. any other non-passing evaluation (low score, aesthetic/ordinary issue,
       missing/parse-failed verdict) -> ``defer_and_continue`` with ``debt=True``.
    5. otherwise -> ``continue_with_warning`` with no reasons.

    Returns ``{"action", "debt", "reasons", "blocking"}``.
    """
    reasons: list[str] = []
    blocking: list[str] = []

    obligations = [str(o) for o in (missing_obligations or []) if str(o).strip()]

    # 1. Planner-level stop.
    if plan_misaligned:
        reasons.append("plan_misaligned")
        blocking.append("plan_misaligned")
    for obligation in obligations:
        token = f"missing_obligation:{obligation}"
        reasons.append(token)
        blocking.append(token)
    if plan_misaligned or obligations:
        return _result(STOP_FOR_REPLAN, reasons, blocking)

    # 2. Mechanical stop: the body cannot be written forward as-is.
    if str(mechanical_status).strip() in _MECHANICAL_FAILURE_STATUSES:
        token = f"mechanical:{mechanical_status}"
        reasons.append(token)
        blocking.append(token)
        return _result(STOP_FOR_REPLAN, reasons, blocking)

    blocking_types, non_blocking_types = _issue_types(evaluation)

    # 3. A defect located inside this chapter: fix the chapter, keep the plan.
    if located_causal_issue:
        reasons.append("located_causal_issue")
    for tag in blocking_types:
        reasons.append(f"blocking_issue:{tag}")
    if located_causal_issue or blocking_types:
        return _result(LOCAL_PATCH_PLAN, reasons, blocking)

    # 4. Ordinary quality problems -> record debt and keep writing.
    if not _evaluation_passes(evaluation):
        reasons.extend(_quality_reasons(evaluation, non_blocking_types))
        return _result(DEFER_AND_CONTINUE, reasons, blocking)

    # 5. Clean and mechanically fine.
    return _result(CONTINUE_WITH_WARNING, [], blocking)
