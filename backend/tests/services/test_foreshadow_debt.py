"""Tests for the foreshadow debt health score (Q4, adapted from QMAI).

Scoring contract (max 100, floor 0):
- status == "planted" and stalled >= 5 chapters since planted_chapter -> critical, -15 each
- status in ("ripening", "ready") (advanced past planted) and stalled >= 10
  chapters since planted_chapter -> warning, -5 each
- resolved foreshadows are skipped entirely
- total unresolved beyond a soft cap of 5 -> -2 per extra item
- render_debt_warning gates on score < 60
"""
from __future__ import annotations

from app.services.foreshadow_manager import (
    CRITICAL_STALL_CHAPTERS,
    DEBT_GATE_THRESHOLD,
    UNRESOLVED_SOFT_CAP,
    WARNING_STALL_CHAPTERS,
    compute_debt_score,
    render_debt_warning,
)


def _fs(
    description: str = "神秘玉佩",
    status: str = "planted",
    planted_chapter: int = 0,
    type: str = "plot",
    resolved_chapter: int | None = None,
) -> dict:
    return {
        "description": description,
        "status": status,
        "planted_chapter": planted_chapter,
        "type": type,
        "resolved_chapter": resolved_chapter,
    }


class TestComputeDebtScore:
    def test_empty_list_is_perfect_health(self):
        debt = compute_debt_score([], current_chapter_idx=10)
        assert debt["score"] == 100
        assert debt["criticals"] == []
        assert debt["warnings"] == []
        assert debt["unresolved"] == 0

    def test_planted_stalled_exactly_five_chapters_is_critical(self):
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=0)],
            current_chapter_idx=CRITICAL_STALL_CHAPTERS,  # age == 5
        )
        assert len(debt["criticals"]) == 1
        assert debt["score"] == 85

    def test_planted_stalled_four_chapters_is_not_critical(self):
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=1)],
            current_chapter_idx=CRITICAL_STALL_CHAPTERS,  # age == 4
        )
        assert debt["criticals"] == []
        assert debt["score"] == 100

    def test_advanced_stalled_ten_chapters_is_warning(self):
        for status in ("ripening", "ready"):
            debt = compute_debt_score(
                [_fs(status=status, planted_chapter=0)],
                current_chapter_idx=WARNING_STALL_CHAPTERS,  # age == 10
            )
            assert len(debt["warnings"]) == 1, status
            assert debt["criticals"] == [], status
            assert debt["score"] == 95, status

    def test_advanced_stalled_nine_chapters_is_not_warning(self):
        debt = compute_debt_score(
            [_fs(status="ripening", planted_chapter=1)],
            current_chapter_idx=WARNING_STALL_CHAPTERS,  # age == 9
        )
        assert debt["warnings"] == []
        assert debt["score"] == 100

    def test_resolved_is_skipped_entirely(self):
        debt = compute_debt_score(
            [_fs(status="resolved", planted_chapter=0, resolved_chapter=3)],
            current_chapter_idx=50,
        )
        assert debt["score"] == 100
        assert debt["unresolved"] == 0
        assert debt["criticals"] == []
        assert debt["warnings"] == []

    def test_unresolved_soft_cap_penalty(self):
        # 7 freshly planted foreshadows: no stall penalties, but 2 over the
        # soft cap of 5 -> -2 each.
        items = [
            _fs(description=f"伏笔{i}", planted_chapter=10)
            for i in range(UNRESOLVED_SOFT_CAP + 2)
        ]
        debt = compute_debt_score(items, current_chapter_idx=10)
        assert debt["unresolved"] == 7
        assert debt["criticals"] == []
        assert debt["warnings"] == []
        assert debt["score"] == 100 - 2 * 2

    def test_score_floor_is_zero(self):
        items = [
            _fs(description=f"伏笔{i}", planted_chapter=0) for i in range(10)
        ]
        debt = compute_debt_score(items, current_chapter_idx=100)
        assert debt["score"] == 0

    def test_entries_carry_description_for_prompt_and_api(self):
        debt = compute_debt_score(
            [_fs(description="灭门旧案", status="planted", planted_chapter=0)],
            current_chapter_idx=20,
        )
        assert debt["criticals"][0]["description"] == "灭门旧案"
        assert debt["criticals"][0]["planted_chapter"] == 0
        assert debt["criticals"][0]["age"] == 20

    def test_negative_age_does_not_penalize(self):
        # Foreshadow planted in a later chapter than current (e.g. outline
        # pre-planting): must not count as stalled.
        debt = compute_debt_score(
            [_fs(status="planted", planted_chapter=30)],
            current_chapter_idx=10,
        )
        assert debt["criticals"] == []
        assert debt["score"] == 100

    def test_accepts_orm_like_objects(self):
        class FakeForeshadow:
            description = "断剑来历"
            status = "planted"
            planted_chapter = 0
            type = "mystery"
            resolved_chapter = None

        debt = compute_debt_score([FakeForeshadow()], current_chapter_idx=8)
        assert len(debt["criticals"]) == 1
        assert debt["criticals"][0]["description"] == "断剑来历"


class TestRenderDebtWarning:
    def test_score_below_threshold_renders_warning(self):
        items = [
            _fs(description=f"超期伏笔{i}", status="planted", planted_chapter=0)
            for i in range(3)
        ]
        debt = compute_debt_score(items, current_chapter_idx=20)
        assert debt["score"] == 55  # 100 - 3*15
        assert debt["score"] < DEBT_GATE_THRESHOLD

        text = render_debt_warning(debt)
        assert text
        assert "55" in text
        assert "超期伏笔0" in text
        assert "禁止新埋" in text

    def test_score_at_or_above_threshold_renders_empty(self):
        items = [_fs(description="单条超期", status="planted", planted_chapter=0)]
        debt = compute_debt_score(items, current_chapter_idx=20)
        assert debt["score"] == 85
        assert render_debt_warning(debt) == ""

    def test_empty_or_malformed_debt_renders_empty(self):
        assert render_debt_warning({}) == ""
        assert render_debt_warning(None) == ""  # type: ignore[arg-type]


class TestContextPackDebtInjection:
    def test_system_prompt_contains_debt_warning_in_foreshadow_section(self):
        from app.services.context_pack import ContextPack

        warning = "【伏笔债务警报】健康分 40/100，本章优先推进或回收既有伏笔，禁止新埋伏笔。"
        pack = ContextPack(foreshadow_debt_warning=warning)
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【伏笔追踪】" in prompt
        assert warning in prompt

    def test_system_prompt_omits_debt_warning_when_empty(self):
        from app.services.context_pack import ContextPack

        pack = ContextPack(foreshadow_debt_warning="")
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "伏笔债务警报" not in prompt
