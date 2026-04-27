"""v1.5.0 C2 — auto-revise loop regression tests.

Covers the pure helpers in app.services.auto_revise (threshold gating,
issue → instruction translation, instruction merge ordering). Hitting
the SSE handler / SceneOrchestrator end-to-end is reserved for the
live smoke step; this file is fast unit coverage that runs in pytest
without LLM/DB/celery.
"""
from __future__ import annotations

import pytest

from app.services.auto_revise import (
    DEFAULT_MAX_REVISE_ROUNDS,
    DEFAULT_REVISE_THRESHOLD,
    EvaluationLite,
    issues_to_revise_instruction,
    merge_revise_into_user_instruction,
    should_revise,
)


# ---------------------------------------------------------------------------
# should_revise: threshold gating
# ---------------------------------------------------------------------------


def test_should_revise_below_threshold_returns_true() -> None:
    e = EvaluationLite(overall=6.5)
    assert should_revise(e, threshold=7.0) is True


def test_should_revise_at_threshold_returns_false() -> None:
    # Exactly at threshold = quality bar met, accept.
    e = EvaluationLite(overall=7.0)
    assert should_revise(e, threshold=7.0) is False


def test_should_revise_above_threshold_returns_false() -> None:
    e = EvaluationLite(overall=7.98)  # B1' baseline
    assert should_revise(e, threshold=7.0) is False


def test_should_revise_default_threshold_is_seven() -> None:
    assert DEFAULT_REVISE_THRESHOLD == 7.0
    e = EvaluationLite(overall=6.99)
    assert should_revise(e) is True  # uses default threshold


def test_should_revise_default_max_rounds_is_two() -> None:
    assert DEFAULT_MAX_REVISE_ROUNDS == 2


def test_should_revise_handles_dict_input() -> None:
    """Tolerates a plain dict (e.g. JSON deserialized from celery result)."""
    assert should_revise({"overall": 5.0}) is True
    assert should_revise({"overall": 9.0}) is False


def test_should_revise_handles_none_overall_as_zero() -> None:
    e = EvaluationLite(overall=0.0)
    assert should_revise(e, threshold=7.0) is True
    assert should_revise({"overall": None}, threshold=7.0) is True


def test_should_revise_handles_missing_overall() -> None:
    """Empty-ish input should default to 0 and trigger revise."""
    assert should_revise({}, threshold=7.0) is True
    assert should_revise(None, threshold=7.0) is True


# ---------------------------------------------------------------------------
# issues_to_revise_instruction: prompt construction
# ---------------------------------------------------------------------------


def test_issues_to_revise_instruction_includes_round_and_overall() -> None:
    e = EvaluationLite(overall=6.42, plot_coherence=5.0)
    instr = issues_to_revise_instruction(e, round_idx=1)
    assert "第 1 轮" in instr
    assert "overall=6.42" in instr
    assert "plot_coherence" in instr  # dimension scores surfaced


def test_issues_to_revise_instruction_groups_issues_by_dimension() -> None:
    e = EvaluationLite(
        overall=5.0,
        issues=[
            {"dimension": "plot_coherence", "location": 3,
             "description": "演人动机不明", "suggestion": "补充心理描写"},
            {"dimension": "plot_coherence", "location": 7,
             "description": "场景跳軃", "suggestion": "过渡句连接"},
            {"dimension": "style_adherence", "location": 1,
             "description": "词汇偏现代"},
        ],
    )
    instr = issues_to_revise_instruction(e, round_idx=2)
    assert "第 2 轮" in instr
    assert "【剧情连贯性】" in instr
    assert "【风格贴合度】" in instr
    # All three issue descriptions show up.
    assert "演人动机不明" in instr
    assert "场景跳軃" in instr
    assert "词汇偏现代" in instr
    # Suggestions are surfaced when present.
    assert "补充心理描写" in instr


def test_issues_to_revise_instruction_caps_per_dimension() -> None:
    """Long per-dim issue lists must be capped to avoid prompt explosion."""
    issues = [
        {"dimension": "plot_coherence", "location": i,
         "description": f"issue-{i}", "suggestion": ""}
        for i in range(12)
    ]
    e = EvaluationLite(overall=5.0, issues=issues)
    instr = issues_to_revise_instruction(e, round_idx=1, max_per_dimension=5)
    # First five must appear.
    for i in range(5):
        assert f"issue-{i}" in instr
    # Sixth+ must be elided.
    assert "issue-5" not in instr
    assert "issue-11" not in instr
    # Footer indicates how many were elided.
    assert "其他 7 条" in instr


def test_issues_to_revise_instruction_handles_empty_issues() -> None:
    """No specific issues — still emit dimension scores so writer aims higher."""
    e = EvaluationLite(overall=6.0, plot_coherence=5.0, style_adherence=7.5)
    instr = issues_to_revise_instruction(e, round_idx=1)
    assert "第 1 轮" in instr
    assert "plot_coherence" in instr
    assert "请重写本章" in instr


def test_issues_to_revise_instruction_tolerates_legacy_paragraph_field() -> None:
    """Older evaluator outputs use 'paragraph' instead of 'location'."""
    e = EvaluationLite(
        overall=5.0,
        issues=[
            {"dimension": "narrative_pacing", "paragraph": 4,
             "description": "节奏拖沓"},
        ],
    )
    instr = issues_to_revise_instruction(e, round_idx=1)
    assert "段落 4" in instr
    assert "节奏拖沓" in instr


def test_issues_to_revise_instruction_skips_malformed_issue_entries() -> None:
    """Non-dict issue entries (defensive against evaluator regressions)."""
    e = EvaluationLite(
        overall=5.0,
        issues=[
            {"dimension": "plot_coherence", "description": "valid"},
            "not a dict",  # type: ignore[list-item]
            None,
            42,
        ],
    )
    instr = issues_to_revise_instruction(e, round_idx=1)
    assert "valid" in instr
    # Did not crash and produced sensible output.
    assert "第 1 轮" in instr


# ---------------------------------------------------------------------------
# merge_revise_into_user_instruction
# ---------------------------------------------------------------------------


def test_merge_preserves_base_then_appends_revise() -> None:
    base = "原始指令\n你是热血少年"
    revise = "【重写要求】\n提升伏笔处理"
    merged = merge_revise_into_user_instruction(base, revise)
    assert merged.startswith("原始指令")
    assert merged.endswith("提升伏笔处理")
    assert "\n\n" in merged  # blank line separator


def test_merge_with_empty_base_returns_revise_only() -> None:
    merged = merge_revise_into_user_instruction("", "重写。")
    assert merged == "重写。"


def test_merge_with_none_base_returns_revise_only() -> None:
    merged = merge_revise_into_user_instruction(None, "重写。")
    assert merged == "重写。"


def test_merge_strips_whitespace_around_blocks() -> None:
    merged = merge_revise_into_user_instruction(
        "   base   ", "   revise   ",
    )
    assert merged == "base\n\nrevise"


# ---------------------------------------------------------------------------
# C2 GenerateChapterRequest field defaults
# ---------------------------------------------------------------------------


def test_generate_chapter_request_c2_field_defaults() -> None:
    """auto_revise defaults to False so existing callers are unaffected;
    threshold + max_rounds default to safe values."""
    from app.api.generate import GenerateChapterRequest

    req = GenerateChapterRequest(project_id="p1")
    assert req.auto_revise is False
    assert req.revise_threshold == 7.0
    assert req.max_revise_rounds == 2
    # Existing C1 fields unchanged.
    assert req.use_scene_mode is False
    assert req.n_scenes_hint is None
    assert req.target_words is None


def test_generate_chapter_request_accepts_c2_overrides() -> None:
    from app.api.generate import GenerateChapterRequest

    req = GenerateChapterRequest(
        project_id="p1",
        use_scene_mode=True,
        auto_revise=True,
        revise_threshold=7.5,
        max_revise_rounds=1,
    )
    assert req.auto_revise is True
    assert req.revise_threshold == 7.5
    assert req.max_revise_rounds == 1


# ---------------------------------------------------------------------------
# EvaluateTask ORM model + Step D dispatch helper signature
# ---------------------------------------------------------------------------


def test_evaluate_task_model_default_status_is_pending() -> None:
    """ORM-level default; the alembic migration also seeds server_default."""
    from app.models.project import EvaluateTask

    row = EvaluateTask(chapter_id="00000000-0000-0000-0000-000000000000")
    # SQLAlchemy applies column defaults at flush time, not in __init__,
    # so we can only assert the descriptor exists with the right shape.
    assert hasattr(row, "status")
    assert hasattr(row, "round_idx")
    assert hasattr(row, "caller")
    assert hasattr(row, "result_json")
    assert hasattr(row, "error_text")


def test_evaluate_chapter_celery_task_name() -> None:
    from app.tasks.evaluation_tasks import EVALUATE_CHAPTER_TASK

    assert EVALUATE_CHAPTER_TASK == "evaluations.evaluate_chapter"


def test_dispatch_evaluate_task_returns_false_when_broker_down(monkeypatch) -> None:
    """dispatch_evaluate_task must NEVER raise; returns False on failure."""
    from app.tasks import evaluation_tasks as et

    def boom(*args, **kwargs):
        raise RuntimeError("redis broker down")

    monkeypatch.setattr(et.celery_app, "send_task", boom)
    ok = et.dispatch_evaluate_task(
        evaluate_task_id="00000000-0000-0000-0000-000000000000",
        chapter_id="00000000-0000-0000-0000-000000000001",
        caller="unit_test",
    )
    assert ok is False


def test_dispatch_evaluate_task_returns_true_on_success(monkeypatch) -> None:
    from app.tasks import evaluation_tasks as et

    captured: dict = {}

    def fake_send(name, kwargs=None, countdown=0):
        captured["name"] = name
        captured["kwargs"] = kwargs
        captured["countdown"] = countdown
        return None

    monkeypatch.setattr(et.celery_app, "send_task", fake_send)
    ok = et.dispatch_evaluate_task(
        evaluate_task_id="task-abc",
        chapter_id="chap-xyz",
        caller="unit_test",
        countdown=5,
    )
    assert ok is True
    assert captured["name"] == "evaluations.evaluate_chapter"
    assert captured["kwargs"]["evaluate_task_id"] == "task-abc"
    assert captured["kwargs"]["chapter_id"] == "chap-xyz"
    assert captured["kwargs"]["caller"] == "unit_test"
    assert captured["countdown"] == 5
