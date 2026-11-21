"""v1.6.0 X4 regression: scene_mode Prometheus metrics are wired.

We import the metrics, hit each instrumentation helper directly, and assert
the counters/histograms increment. Avoids real LLM/DB so it runs in <50ms.
"""

import pytest

from app.observability.metrics import (
    SCENE_PLAN_FALLBACK_TOTAL,
    SCENE_COUNT_PER_CHAPTER,
    SCENE_REVISE_ROUND_TOTAL,
)


def _counter_value(counter, **labels):
    try:
        return float(counter.labels(**labels)._value.get())
    except Exception:
        return 0.0


def _hist_count(hist):
    try:
        return float(hist._sum.get())
    except Exception:
        return 0.0


def test_scene_plan_fallback_counter_increments():
    from app.services.scene_orchestrator import _x4_inc_fallback
    before = _counter_value(SCENE_PLAN_FALLBACK_TOTAL, reason="unparseable")
    _x4_inc_fallback("unparseable")
    _x4_inc_fallback("unparseable")
    _x4_inc_fallback("too_few")
    after = _counter_value(SCENE_PLAN_FALLBACK_TOTAL, reason="unparseable")
    too_few = _counter_value(SCENE_PLAN_FALLBACK_TOTAL, reason="too_few")
    assert after - before == 2.0
    assert too_few >= 1.0


def test_scene_count_histogram_observes():
    from app.services.scene_orchestrator import _x4_observe_scene_count
    before = _hist_count(SCENE_COUNT_PER_CHAPTER)
    _x4_observe_scene_count(4)
    _x4_observe_scene_count(5)
    after = _hist_count(SCENE_COUNT_PER_CHAPTER)
    assert after - before == 9.0


def test_scene_revise_round_counter_increments():
    from app.api.generate import _x4_inc_revise
    before_scored = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="scored")
    before_skip = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="skipped")
    before_rev = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="revised")
    _x4_inc_revise("scored")
    _x4_inc_revise("scored")
    _x4_inc_revise("skipped")
    _x4_inc_revise("revised")
    after_scored = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="scored")
    after_skip = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="skipped")
    after_rev = _counter_value(SCENE_REVISE_ROUND_TOTAL, outcome="revised")
    assert after_scored - before_scored == 2.0
    assert after_skip - before_skip == 1.0
    assert after_rev - before_rev == 1.0
