"""Defect-class immunity (docs/DEFECT_GOVERNANCE.md): era/register bleed,
scene-seam duplication, and the recurring-defect ledger."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.chapter_quality_gate as cqg
from app.services.chinese_prose_mechanics_checker import (
    ChineseProseMechanicsReport,
    aggregate_recurring_defects,
    analyze_chinese_prose_mechanics,
    build_generation_preflight_prompt,
    classify_genre_register,
    _seam_duplication,
)
from app.services.narrative_contract import WRITER_CONTRACT_PROMPT
from app.services.narrative_quality_gates import CHINESE_PROSE_MECHANICS_PROMPT


NEAR_FUTURE_ERA_BLEED_TEXT = "约莫两炷香工夫，他把巷口那台旧终端修好了。"

SEAM_DUPLICATION_TEXT = (
    "电话里的声音在那一刻断了，听筒里只剩下细密的电流声。\n"
    "他愣了几秒。电话里的声音就在那一刻断了，听筒里只剩下细密的电流声。"
)

SEAM_DISTINCT_TEXT = (
    "他推开门走进院子，雨水顺着屋檐往下淌。\n"
    "屋里的灯还亮着，桌上摆着没吃完的饭。"
)

SEAM_QUOTED_ECHO_TEXT = (
    "“把钥匙放在桌上，转身出去，不要回头。”他说。\n"
    "她重复了一遍：“把钥匙放在桌上，转身出去，不要回头。”"
)


# --- era/register bleed ---


def test_era_detector_flags_period_time_terms_under_modern_genre() -> None:
    report = analyze_chinese_prose_mechanics(
        NEAR_FUTURE_ERA_BLEED_TEXT, genre_hint="近未来科幻"
    )
    assert report.era_register_class == "modern"
    assert report.era_register_conflict_count >= 1
    assert "两炷香" in report.era_register_conflicts


def test_era_detector_allows_period_terms_under_period_genre() -> None:
    report = analyze_chinese_prose_mechanics(
        NEAR_FUTURE_ERA_BLEED_TEXT, genre_hint="古风仙侠"
    )
    assert report.era_register_class == "period"
    assert report.era_register_conflict_count == 0


def test_era_detector_flags_modern_terms_under_xianxia_genre() -> None:
    report = analyze_chinese_prose_mechanics("十分钟后他收了剑。", genre_hint="仙侠")
    assert report.era_register_class == "period"
    assert report.era_register_conflict_count >= 1
    assert "分钟" in report.era_register_conflicts


def test_era_detector_inactive_for_unknown_or_ambiguous_genre() -> None:
    assert analyze_chinese_prose_mechanics(NEAR_FUTURE_ERA_BLEED_TEXT).era_register_conflict_count == 0
    assert (
        analyze_chinese_prose_mechanics(
            NEAR_FUTURE_ERA_BLEED_TEXT, genre_hint="悬疑"
        ).era_register_conflict_count
        == 0
    )
    assert classify_genre_register(None) == ""
    assert classify_genre_register("悬疑") == ""
    # Conflicting signals must not guess an era.
    assert classify_genre_register("现代修仙都市") == ""


def test_era_detector_guards_era_neutral_survivals() -> None:
    # 下午时分 must not trip the 地支+时 pattern; 小时候 must not trip 小时.
    assert (
        analyze_chinese_prose_mechanics("下午时分他到了公司。", genre_hint="都市").era_register_conflict_count
        == 0
    )
    assert (
        analyze_chinese_prose_mechanics("小时候他常来这座山。", genre_hint="仙侠").era_register_conflict_count
        == 0
    )


def test_era_hits_are_warn_level_and_do_not_flip_passed() -> None:
    without_hint = analyze_chinese_prose_mechanics(NEAR_FUTURE_ERA_BLEED_TEXT)
    with_hint = analyze_chinese_prose_mechanics(
        NEAR_FUTURE_ERA_BLEED_TEXT, genre_hint="近未来科幻"
    )
    assert with_hint.era_register_conflict_count >= 1
    assert with_hint.passed == without_hint.passed


# --- scene-seam duplication ---


def test_seam_detector_flags_live_restatement_example() -> None:
    report = analyze_chinese_prose_mechanics(SEAM_DUPLICATION_TEXT)
    assert report.seam_duplication_count >= 1
    assert report.seam_duplication_pairs
    assert "电流" in report.seam_duplication_pairs[0]


def test_seam_detector_clean_on_distinct_sentences() -> None:
    count, pairs = _seam_duplication(SEAM_DISTINCT_TEXT)
    assert count == 0
    assert pairs == []


def test_seam_detector_ignores_quoted_dialogue_echo() -> None:
    count, pairs = _seam_duplication(SEAM_QUOTED_ECHO_TEXT)
    assert count == 0
    assert pairs == []


GOOD_CONFLICT_TEXT = """
邱成手背青筋微突，死死按住本子：“只露一行。多半个字，我立马撕了。”

“可以。”陈青没废话，“认错我赔钱。认对，这本东西今晚谁也别想碰，明早市书会见。”

陈青逼近半步。

邱成眼神瞬间警惕：“退回去。”

陈青没退，目光盯死那行字：“‘青儿药照前方，德安堂欠二钱’。”

“街上药单都这么写，算什么铁证？”

“德安堂老周记账，规矩是年月打头，病人在后。”陈青声音冷硬，“只有我祖母去赊药，怕老眼昏花抓错，才会把我的名字强行顶在最前头。”
"""

SEAM_TAIL = (
    "\n窗外的雨把巷子浇得发亮，檐口的水线连成一片。"
    "\n窗外的大雨把巷子浇得发亮，檐口的水线连成了一片。"
)


def test_seam_hits_are_warn_level_and_do_not_flip_passed() -> None:
    baseline = analyze_chinese_prose_mechanics(GOOD_CONFLICT_TEXT)
    with_seam = analyze_chinese_prose_mechanics(GOOD_CONFLICT_TEXT + SEAM_TAIL)
    assert baseline.passed is True
    assert with_seam.seam_duplication_count >= 1
    # WARN-level: an otherwise passing chapter still passes, but the penalty
    # rises so rewrite rounds are steered to merge/delete the restatement.
    assert with_seam.passed is True
    assert cqg._quality_penalty(with_seam) > cqg._quality_penalty(baseline)


# --- recurring-defect ledger ---


def _history_with_recurrence() -> list[list[dict]]:
    return [
        [{"violation_type": "information_rule_violation", "description": "证" * 120}],
        [{"description": "[time_rule_violation] 只出现一次"}],
        [{"violation_type": "information_rule_violation", "description": "无来源直接定案"}],
        [],
        [{"violation_type": "information_rule_violation"}],
    ]


def test_ledger_surfaces_tags_recurring_in_two_or_more_chapters() -> None:
    agg = aggregate_recurring_defects(_history_with_recurrence())
    assert "information_rule_violation" in agg
    entry = agg["information_rule_violation"]
    assert entry["chapters"] == 3
    # repair_action comes from QUALITY_GATE_RULES for known tags.
    assert "来源" in entry["repair_action"]
    # example truncated to <=80 chars.
    assert len(entry["example"]) <= 80
    # A tag seen in only one chapter is not surfaced.
    assert "time_rule_violation" not in agg


def test_ledger_empty_without_recurrence() -> None:
    assert aggregate_recurring_defects([[{"violation_type": "time_rule_violation"}]]) == {}
    assert aggregate_recurring_defects(None) == {}


def test_preflight_prompt_carries_recurring_defect_block() -> None:
    prompt = build_generation_preflight_prompt(
        None, recurring_issue_history=_history_with_recurrence()
    )
    assert "本项目高发问题" in prompt
    assert "information_rule_violation" in prompt
    assert "time_rule_violation" not in prompt


def test_preflight_prompt_omits_block_without_history() -> None:
    prompt = build_generation_preflight_prompt(None)
    assert "本项目高发问题" not in prompt


# --- prompt-content assertions ---


def test_preflight_prompt_carries_era_and_seam_rules() -> None:
    report = analyze_chinese_prose_mechanics(
        NEAR_FUTURE_ERA_BLEED_TEXT, genre_hint="近未来科幻"
    )
    prompt = build_generation_preflight_prompt(report)
    assert "era_register_consistency" in prompt
    assert "era_register_conflict=1" in prompt
    assert "seam_duplication_count 必须为 0" in prompt
    assert "时代语域越界词" in prompt and "两炷香" in prompt


def test_contract_prompts_carry_era_and_seam_bullets() -> None:
    assert "era_register_consistency" in CHINESE_PROSE_MECHANICS_PROMPT
    assert "场景接缝去重" in CHINESE_PROSE_MECHANICS_PROMPT
    assert "时代语域一致性" in WRITER_CONTRACT_PROMPT
    assert "场景接缝去重" in WRITER_CONTRACT_PROMPT


def test_quality_penalty_counts_era_and_seam_metrics() -> None:
    base = cqg._quality_penalty(ChineseProseMechanicsReport())
    with_era = ChineseProseMechanicsReport(era_register_conflict_count=2)
    with_seam = ChineseProseMechanicsReport(seam_duplication_count=1)
    assert cqg._quality_penalty(with_era) == base + 2 * 320
    assert cqg._quality_penalty(with_seam) == base + 300


def test_rewrite_prompt_carries_era_and_seam_instructions() -> None:
    report = analyze_chinese_prose_mechanics(
        NEAR_FUTURE_ERA_BLEED_TEXT + "\n" + SEAM_DUPLICATION_TEXT,
        genre_hint="近未来科幻",
    )
    content = cqg._build_rewrite_user_content(
        text="正文",
        initial_report=report,
        round_idx=1,
        previous_attempts=[],
    )
    assert "era_register_consistency" in content
    assert "seam_duplication" in content
    assert '"era_register_conflicts"' in content
    assert '"seam_duplication_pairs"' in content
    assert "两炷香" in content  # detected terms reach the rewrite model


# --- gate plumbing ---


class _FakeGenreDB:
    """Stub AsyncSession: returns a project row for genre resolution."""

    def __init__(self, project) -> None:
        self._project = project

    async def get(self, model, pk):  # noqa: ANN001 - signature mirrors AsyncSession
        return self._project

    async def execute(self, stmt):  # noqa: ANN001
        raise RuntimeError("no queries expected beyond genre lookup")


@pytest.mark.asyncio
async def test_quality_gate_resolves_genre_from_project_row() -> None:
    project = SimpleNamespace(genre="近未来科幻", genre_profile_code="scifi")
    result = await cqg.apply_chapter_quality_gate(
        text=NEAR_FUTURE_ERA_BLEED_TEXT,
        db=_FakeGenreDB(project),
        project_id="proj",
        chapter_id="ch",
        skip_polish=True,
    )
    assert result.initial_report.era_register_class == "modern"
    assert result.initial_report.era_register_conflict_count >= 1


@pytest.mark.asyncio
async def test_quality_gate_explicit_genre_hint_wins_and_stub_db_is_safe() -> None:
    # Explicit hint: no DB lookup needed; SimpleNamespace db must not break.
    result = await cqg.apply_chapter_quality_gate(
        text=NEAR_FUTURE_ERA_BLEED_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        skip_polish=True,
        genre_hint="古风仙侠",
    )
    assert result.initial_report.era_register_class == "period"
    assert result.initial_report.era_register_conflict_count == 0


@pytest.mark.asyncio
async def test_quality_gate_stub_db_without_hint_keeps_detector_inactive() -> None:
    result = await cqg.apply_chapter_quality_gate(
        text=NEAR_FUTURE_ERA_BLEED_TEXT,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
        skip_polish=True,
    )
    assert result.initial_report.era_register_class == ""
    assert result.initial_report.era_register_conflict_count == 0
