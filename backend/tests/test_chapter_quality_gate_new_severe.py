"""Structural repair must not introduce a NEW severe (critical/high) defect.

The bounded rewrite only cleans output structure. Before this rule the gate
accepted any candidate whose total penalty dropped (or that simply passed),
even when the candidate newly leaked story-bible/meta material or broke POV /
mundane logic / resource continuity / action causality. These tests pin the
"never introduce a new high/critical issue" rule and the tier constants.

No database, no model: the mechanics checker and the prompt runner are both
replaced with fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.chapter_quality_gate as cqg
from app.services.chinese_prose_mechanics_checker import ChineseProseMechanicsReport


PROSE = (
    "她顿了顿,把六本台账锁进柜子。\n"
    "“明早一起核对。”她说。\n"
    "陈青把灯往外挪了一点,雨声压住了院里的脚步。“先核对。”"
)
# The original leaks one standalone structure tag; the candidate is the same
# prose with that tag removed, so "prose lines unchanged" still holds.
ORIGINAL = "[CH-12]\n" + PROSE
CANDIDATE = PROSE


def _reports(*, passed: bool, **counts: int) -> ChineseProseMechanicsReport:
    return ChineseProseMechanicsReport(passed=passed, **counts)


def _sequence_checker(monkeypatch, reports):
    """Return report[i] on the i-th call (initial analysis, then each round)."""
    calls = {"n": 0}

    def fake_analyze(text, genre_hint=None):
        index = min(calls["n"], len(reports) - 1)
        calls["n"] += 1
        return reports[index]

    monkeypatch.setattr(cqg, "analyze_chinese_prose_mechanics", fake_analyze)
    return calls


def _stub_rewrite(monkeypatch, candidate_text: str = CANDIDATE) -> None:
    async def fake_run_text_prompt(**kwargs):
        return SimpleNamespace(text=candidate_text)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)


# --------------------------------------------------------------------------
# Pure unit coverage of the rule itself
# --------------------------------------------------------------------------

def test_new_severe_findings_reports_critical_before_high_in_constant_order() -> None:
    original = SimpleNamespace()
    candidate = SimpleNamespace(
        story_bible_leakage_count=1,
        meta_structure_leakage_count=2,
        limited_pov_leak_count=1,
        action_causality_count=3,
    )
    assert cqg._new_severe_findings(original, candidate) == [
        "critical:story_bible_leakage_count",
        "critical:meta_structure_leakage_count",
        "high:limited_pov_leak_count",
        "high:action_causality_count",
    ]


def test_new_severe_findings_ignores_equal_and_lower_counts() -> None:
    original = SimpleNamespace(
        story_bible_leakage_count=1,
        meta_structure_leakage_count=2,
        limited_pov_leak_count=2,
        mundane_logic_violation_count=1,
        resource_continuity_count=1,
        action_causality_count=1,
    )
    equal = SimpleNamespace(
        story_bible_leakage_count=1,
        meta_structure_leakage_count=2,
        limited_pov_leak_count=2,
        mundane_logic_violation_count=1,
        resource_continuity_count=1,
        action_causality_count=1,
    )
    lower = SimpleNamespace(
        story_bible_leakage_count=0,
        meta_structure_leakage_count=0,
        limited_pov_leak_count=0,
        mundane_logic_violation_count=0,
        resource_continuity_count=0,
        action_causality_count=0,
    )
    assert cqg._new_severe_findings(original, equal) == []
    assert cqg._new_severe_findings(original, lower) == []


def test_new_severe_findings_does_not_use_the_old_generic_word() -> None:
    findings = cqg._new_severe_findings(
        SimpleNamespace(), SimpleNamespace(limited_pov_leak_count=1)
    )
    assert findings == ["high:limited_pov_leak_count"]
    assert not any("penalty" in item or "no_improvement" in item for item in findings)


def test_new_severe_findings_tolerates_missing_fields() -> None:
    # A report object without any of the counters (future rename) must not raise.
    assert cqg._new_severe_findings(SimpleNamespace(), SimpleNamespace()) == []
    assert cqg._new_severe_findings(
        SimpleNamespace(story_bible_leakage_count=0),
        SimpleNamespace(story_bible_leakage_count=1),
    ) == ["critical:story_bible_leakage_count"]
    # None is treated as zero rather than raising a TypeError on comparison.
    assert cqg._new_severe_findings(
        SimpleNamespace(story_bible_leakage_count=None),
        SimpleNamespace(story_bible_leakage_count=1),
    ) == ["critical:story_bible_leakage_count"]
    # Non-severe counters are out of scope and never raise.
    assert cqg._new_severe_findings(
        SimpleNamespace(repeated_rare_phrase_count=0),
        SimpleNamespace(repeated_rare_phrase_count=99),
    ) == []


def test_severity_tiers_are_declared_as_module_constants() -> None:
    assert cqg.CRITICAL_REPORT_FIELDS == (
        "story_bible_leakage_count",
        "meta_structure_leakage_count",
    )
    assert cqg.HIGH_REPORT_FIELDS == (
        "limited_pov_leak_count",
        "mundane_logic_violation_count",
        "resource_continuity_count",
        "action_causality_count",
    )
    # Every declared field must exist on the real report.
    report = ChineseProseMechanicsReport()
    for name in cqg.CRITICAL_REPORT_FIELDS + cqg.HIGH_REPORT_FIELDS:
        assert hasattr(report, name), name


# --------------------------------------------------------------------------
# _candidate_is_acceptable: the rule wins over a better total score
# --------------------------------------------------------------------------

def test_acceptable_candidate_rejects_new_story_bible_leak_despite_better_score() -> None:
    original = _reports(passed=False, meta_structure_leakage_count=1)
    candidate = _reports(
        passed=True, meta_structure_leakage_count=0, story_bible_leakage_count=1
    )
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=CANDIDATE,
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert accepted is False
    assert reason == "new_severe:critical:story_bible_leakage_count"
    # Sanity: the candidate really was the better-scoring one.
    assert cqg._quality_penalty(candidate) < cqg._quality_penalty(original)


def test_acceptable_candidate_rejects_new_pov_leak() -> None:
    original = _reports(passed=False, meta_structure_leakage_count=1)
    candidate = _reports(passed=False, meta_structure_leakage_count=0, limited_pov_leak_count=1)
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=CANDIDATE,
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert accepted is False
    assert reason == "new_severe:high:limited_pov_leak_count"


def test_acceptable_candidate_allows_unchanged_pov_leak() -> None:
    original = _reports(passed=False, meta_structure_leakage_count=1, limited_pov_leak_count=2)
    candidate = _reports(passed=True, meta_structure_leakage_count=0, limited_pov_leak_count=2)
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=CANDIDATE,
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert (accepted, reason) == (True, None)


def test_acceptable_candidate_allows_fewer_severe_findings() -> None:
    original = _reports(
        passed=False,
        meta_structure_leakage_count=1,
        story_bible_leakage_count=2,
        limited_pov_leak_count=1,
    )
    candidate = _reports(passed=True, meta_structure_leakage_count=0)
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=CANDIDATE,
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert (accepted, reason) == (True, None)


def test_observational_counters_do_not_trip_the_severe_rule() -> None:
    original = _reports(passed=True)
    candidate = _reports(passed=True, repeated_rare_phrase_count=50, quote_count=9)
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=CANDIDATE,
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert (accepted, reason) == (True, None)


def test_existing_hard_checks_still_run_first() -> None:
    # prose_changed must win over the severe rule when both would apply.
    original = _reports(passed=False, meta_structure_leakage_count=1)
    candidate = _reports(
        passed=False, meta_structure_leakage_count=1, story_bible_leakage_count=3
    )
    accepted, reason = cqg._candidate_is_acceptable(
        original_text=ORIGINAL,
        candidate_text=PROSE.replace("六本", "七本"),
        candidate_report=candidate,
        original_report=original,
        best_report=original,
        best_text=ORIGINAL,
    )
    assert (accepted, reason) == (False, "prose_changed")


# --------------------------------------------------------------------------
# Full gate: the fixed initial report is what the rule compares against
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_blocks_rewrite_that_adds_story_bible_leak(monkeypatch) -> None:
    _stub_rewrite(monkeypatch)
    _sequence_checker(
        monkeypatch,
        [
            _reports(passed=False, meta_structure_leakage_count=1),
            _reports(passed=True, meta_structure_leakage_count=0, story_bible_leakage_count=1),
        ],
    )

    result = await cqg.apply_chapter_quality_gate(text=ORIGINAL, db=None, max_rewrite_rounds=1)

    assert result.status == "blocked"
    assert result.final_text == ORIGINAL
    assert result.warning_reason == "new_severe:critical:story_bible_leakage_count"
    attempt = result.rewrite_attempts[0]
    assert attempt["accepted"] is False
    assert attempt["reason"] == "new_severe:critical:story_bible_leakage_count"
    assert attempt["new_severe"] == ["critical:story_bible_leakage_count"]
    assert attempt["passed"] is True
    assert attempt["score"] == 0


@pytest.mark.asyncio
async def test_gate_blocks_rewrite_that_adds_pov_leak(monkeypatch) -> None:
    _stub_rewrite(monkeypatch)
    _sequence_checker(
        monkeypatch,
        [
            _reports(passed=False, meta_structure_leakage_count=1, limited_pov_leak_count=1),
            _reports(passed=False, meta_structure_leakage_count=0, limited_pov_leak_count=2),
        ],
    )

    result = await cqg.apply_chapter_quality_gate(text=ORIGINAL, db=None, max_rewrite_rounds=1)

    assert result.status == "blocked"
    assert result.final_text == ORIGINAL
    assert result.rewrite_attempts[0]["reason"] == "new_severe:high:limited_pov_leak_count"
    assert result.rewrite_attempts[0]["new_severe"] == ["high:limited_pov_leak_count"]


@pytest.mark.asyncio
async def test_gate_compares_against_initial_report_not_rolling_current(monkeypatch) -> None:
    """The baseline is the fixed original report, never the rolling one.

    Round 1 drops story-bible leaks from 2 to 0. Round 2 restores one leak: it
    is still BELOW the original's 2, so it is not a new severe defect and must
    be accepted. Passing the rolling ``current_report`` instead of
    ``initial_report`` would wrongly reject round 2 as new_severe.
    """
    _stub_rewrite(monkeypatch)
    _sequence_checker(
        monkeypatch,
        [
            _reports(passed=False, meta_structure_leakage_count=1, story_bible_leakage_count=2),
            _reports(passed=False, meta_structure_leakage_count=0, story_bible_leakage_count=0),
            _reports(passed=True, meta_structure_leakage_count=0, story_bible_leakage_count=1),
        ],
    )

    result = await cqg.apply_chapter_quality_gate(text=ORIGINAL, db=None, max_rewrite_rounds=2)

    assert [a["accepted"] for a in result.rewrite_attempts] == [True, True]
    assert result.rewrite_attempts[0]["new_severe"] == []
    assert result.rewrite_attempts[1]["new_severe"] == []
    assert result.rewrite_attempts[1]["reason"] is None
    assert result.status == "passed"
    assert result.final_text == CANDIDATE


@pytest.mark.asyncio
async def test_gate_still_accepts_clean_structural_repair(monkeypatch) -> None:
    _stub_rewrite(monkeypatch)
    _sequence_checker(
        monkeypatch,
        [
            _reports(passed=False, meta_structure_leakage_count=1),
            _reports(passed=True, meta_structure_leakage_count=0),
        ],
    )

    result = await cqg.apply_chapter_quality_gate(text=ORIGINAL, db=None, max_rewrite_rounds=1)

    assert result.status == "passed"
    assert result.final_text == CANDIDATE
    assert result.rewrite_attempts[0]["new_severe"] == []
    assert result.rewrite_attempts[0]["accepted"] is True