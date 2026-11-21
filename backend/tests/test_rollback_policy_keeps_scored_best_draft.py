"""Regression: auto-revise rollback must not discard a scored draft for an
unscored (or lower-scored) baseline.

Live incident (2026-07-26): an explicit chapter regeneration improved across
3 auto-revise rounds (6.5 -> 7.1 -> 7.3 -> 7.9) but missed the 8.2
revise_threshold. The rollback branch restored the baseline -- an OLD text
whose only evaluation row was a 0.00 parse-failure relic, i.e. it had NEVER
been validly scored. A scored-7.9 draft was silently replaced by an unscored
one and survived only as an inactive chapter_versions row.

Policy under test (app.api.generate.resolve_rollback_decision):
- baseline unscored + best draft REAL-scored >= ROLLBACK_KEEP_MIN_SCORE
  -> keep the best draft (as status=draft), kept_best_draft=true.
- baseline validly scored -> roll back only if baseline >= best (equal ->
  conservative baseline); otherwise keep the best draft.
- best draft below the floor -> existing rollback behavior, always.
- empty baseline -> legacy resolve_rollback_text contract untouched
  (persist_on_block keeps the draft; persist off wipes).

Baseline-score attribution (_latest_valid_baseline_overall): an evaluation
row counts for the baseline's CURRENT text only when overall > 0 (parse
failures persist sentinel 0.0 rows) and its created_at falls in
[baseline_updated_at, run_started_at] -- older rows scored an earlier text,
newer rows scored this run's drafts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.generate import (
    _latest_valid_baseline_overall,
    resolve_rollback_decision,
)

BASELINE = "旧版基线正文（从未有效评分）。" * 20
BEST = "本次生成中评分最高的草稿正文。" * 20
CURRENT = "最后一轮（可能不是最佳）的草稿正文。" * 20
FLOOR = 7.0


def _decide(**overrides):
    kwargs = dict(
        baseline_text=BASELINE,
        current_text=CURRENT,
        persist_on_block=True,
        best_draft_text=BEST,
        best_draft_score=7.9,
        baseline_score=None,
        keep_min_score=FLOOR,
    )
    kwargs.update(overrides)
    return resolve_rollback_decision(**kwargs)


# ---------------------------------------------------------------------------
# resolve_rollback_decision
# ---------------------------------------------------------------------------

def test_incident_scored_best_draft_beats_unscored_baseline():
    # The live incident: best=7.9 with a real score, baseline unscored.
    text, kept, reason = _decide()
    assert text == BEST
    assert kept is True  # feeds the rollback_applied event flag
    assert reason == "baseline_unscored"


def test_valid_baseline_score_above_best_rolls_back():
    text, kept, reason = _decide(baseline_score=8.5)
    assert text == BASELINE
    assert kept is False
    assert reason is None


def test_valid_baseline_score_below_best_keeps_draft():
    text, kept, reason = _decide(baseline_score=6.0)
    assert text == BEST
    assert kept is True
    assert reason == "best_beats_baseline"


def test_equal_scores_keep_baseline_conservative():
    text, kept, reason = _decide(baseline_score=7.9)
    assert text == BASELINE
    assert kept is False
    assert reason is None


def test_best_below_floor_rolls_back_even_if_baseline_unscored():
    text, kept, reason = _decide(best_draft_score=6.5)
    assert text == BASELINE
    assert kept is False
    assert reason is None


def test_no_scored_best_draft_rolls_back():
    # All evaluations parse-failed -> best never tracked.
    text, kept, reason = _decide(best_draft_text=None, best_draft_score=None)
    assert text == BASELINE
    assert kept is False
    assert reason is None


def test_empty_baseline_persist_on_block_contract_unchanged():
    # FRESH chapter + persist-on-block keeps the current saved draft via the
    # legacy resolve_rollback_text path (kept flag true, no policy reason),
    # even when a scored best draft exists.
    text, kept, reason = _decide(baseline_text="")
    assert text == CURRENT
    assert kept is True
    assert reason is None


def test_empty_baseline_persist_disabled_still_wipes():
    text, kept, reason = _decide(baseline_text="", persist_on_block=False)
    assert text == ""
    assert kept is False
    assert reason is None


# ---------------------------------------------------------------------------
# _latest_valid_baseline_overall (baseline-score attribution)
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)  # baseline saved
_RUN = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)  # run start


def test_attribution_parse_failure_relic_is_not_a_score():
    # The incident baseline: only a 0.00 parse-failure row in the window.
    rows = [(0.0, _T0 + timedelta(hours=1))]
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=_T0, run_started_at=_RUN,
    ) is None


def test_attribution_valid_row_in_window_counts():
    rows = [
        (7.9, _RUN + timedelta(minutes=30)),  # this run's draft -> excluded
        (8.5, _T0 + timedelta(hours=2)),      # baseline's own score
        (9.0, _T0 - timedelta(days=1)),       # scored an earlier text
    ]
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=_T0, run_started_at=_RUN,
    ) == 8.5


def test_attribution_stale_and_run_rows_excluded():
    rows = [
        (7.9, _RUN + timedelta(minutes=5)),  # this run's draft
        (9.0, _T0 - timedelta(seconds=1)),   # predates baseline save
    ]
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=_T0, run_started_at=_RUN,
    ) is None


def test_attribution_naive_datetimes_treated_as_utc():
    rows = [(8.1, (_T0 + timedelta(hours=1)).replace(tzinfo=None))]
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=_T0.replace(tzinfo=None), run_started_at=_RUN,
    ) == 8.1


def test_attribution_unknown_timestamps_mean_unscored():
    rows = [(8.5, _T0 + timedelta(hours=1))]
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=None, run_started_at=_RUN,
    ) is None
    assert _latest_valid_baseline_overall(
        rows, baseline_updated_at=_T0, run_started_at=None,
    ) is None
