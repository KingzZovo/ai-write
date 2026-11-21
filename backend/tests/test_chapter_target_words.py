from __future__ import annotations

from app.services.chapter_target_words import (
    CHAPTER_DEFAULT_WORD_COUNT,
    LEGACY_CHAPTER_DEFAULT_WORD_COUNT,
    build_planning_scale,
    is_legacy_chapter_target_word_count,
    resolve_chapter_target_word_count,
    resolve_chapter_words,
    resolve_chapters_per_volume_pref,
    resolve_chapters_per_volume_scale,
    resolve_volume_chapter_range,
)


def test_chapter_target_word_defaults_shift_to_4000() -> None:
    assert CHAPTER_DEFAULT_WORD_COUNT == 4000
    assert LEGACY_CHAPTER_DEFAULT_WORD_COUNT == 50_000


def test_legacy_50000_resolves_to_4000_without_project_override() -> None:
    assert is_legacy_chapter_target_word_count(50_000) is True
    assert resolve_chapter_target_word_count(50_000, None) == 4000


def test_project_setting_overrides_legacy_50000() -> None:
    assert resolve_chapter_target_word_count(50_000, 3200) == 3200


def test_real_user_target_is_preserved() -> None:
    assert is_legacy_chapter_target_word_count(7200) is False
    assert resolve_chapter_target_word_count(7200, 3200) == 7200


def test_celery_chapter_target_words_resolves_legacy_50k() -> None:
    from app.tasks.knowledge_tasks import _resolve_task_chapter_target_words

    assert _resolve_task_chapter_target_words({"target_words": None}, 50_000, {"target_chapter_words": 6000}) == 6000
    assert _resolve_task_chapter_target_words({}, 50_000, {}) == 4000
    assert _resolve_task_chapter_target_words({"target_words": 3000}, 50_000, {}) == 3000
    assert _resolve_task_chapter_target_words(None, 7000, None) == 7000


def test_celery_chapter_branch_wired_to_resolver() -> None:
    """Guard the call-site wiring: the original bug was exactly an unwired resolver.

    Source-assertion tripwire (same approach as the max_tokens guard in
    tests/services/test_chapter_evaluator_prompt.py). Whitespace is stripped
    because the call site is formatted across multiple lines.
    """
    import inspect
    import re

    from app.tasks import generation_tasks

    src = re.sub(r"\s+", "", inspect.getsource(generation_tasks))
    assert (
        "generated_chapter_target_words="
        "_resolve_task_chapter_target_words(params,ch.target_word_count,project_settings)"
    ) in src


# ---------------------------------------------------------------------------
# 每章字数 / 每卷章数:规范键 ⇄ 简写键双侧回退
# ---------------------------------------------------------------------------
#
# 两套历史键名各被某一层写过:前端设置弹窗写 ``target_chapter_words`` /
# ``chapters_per_volume_min|max|target``,探针与脚本写 ``chapter_words`` /
# ``chapters_per_volume``。下面每条用例都锁一侧读法与优先级。


def test_chapter_words_shorthand_only_is_read() -> None:
    assert resolve_chapter_words({"chapter_words": 2000}) == 2000


def test_chapter_words_canonical_only_is_read() -> None:
    assert resolve_chapter_words({"target_chapter_words": 2500}) == 2500


def test_chapter_words_canonical_wins_over_shorthand() -> None:
    assert (
        resolve_chapter_words({"target_chapter_words": 2500, "chapter_words": 2000})
        == 2500
    )


def test_chapter_words_fall_back_to_default_and_skip_legacy_50k() -> None:
    assert resolve_chapter_words({}) == CHAPTER_DEFAULT_WORD_COUNT
    assert resolve_chapter_words(None) == CHAPTER_DEFAULT_WORD_COUNT
    # 历史缺省 50000 不是用户偏好:让简写键 / 默认值接手。
    assert (
        resolve_chapter_words({"target_chapter_words": 50_000, "chapter_words": 2000})
        == 2000
    )
    assert resolve_chapter_words({"target_chapter_words": 50_000}) == CHAPTER_DEFAULT_WORD_COUNT


def test_chapters_per_volume_scale_reads_both_key_sets() -> None:
    assert resolve_chapters_per_volume_scale({"chapters_per_volume": 100}) == (100, 100, 100)
    assert resolve_chapters_per_volume_scale(
        {
            "chapters_per_volume_min": 100,
            "chapters_per_volume_max": 100,
            "chapters_per_volume_target": 100,
        }
    ) == (100, 100, 100)
    assert resolve_chapters_per_volume_scale({}) == (100, 200, 150)


def test_chapters_per_volume_scale_prefers_canonical_keys() -> None:
    assert resolve_chapters_per_volume_scale(
        {
            "chapters_per_volume": 100,
            "chapters_per_volume_min": 20,
            "chapters_per_volume_max": 30,
            "chapters_per_volume_target": 25,
        }
    ) == (20, 30, 25)


def test_scale_from_settings_is_500_chapters_in_5_volumes_of_100() -> None:
    """100 万字 + 每章 2000 字 + 每卷 100 章 → 500 章 / 5 卷 / 每卷 100 章。

    规范键与简写键写同一组值必须得到同一结果 —— 规划链两端各读各键正是本缺陷的根因:
    只读规范键时 100 万字曾被算成 2 卷 × 125 章(每章按 4000 字)。
    """
    expected = {
        "target_word_count": 1_000_000,
        "n_chapters": 500,
        "n_volumes": 5,
        "chapters_per_volume": 100,
        "chapter_words": 2000,
    }
    assert build_planning_scale(
        1_000_000,
        {
            "target_chapter_words": 2000,
            "chapters_per_volume_min": 100,
            "chapters_per_volume_max": 100,
            "chapters_per_volume_target": 100,
        },
    ) == expected
    assert build_planning_scale(
        1_000_000, {"chapter_words": 2000, "chapters_per_volume": 100}
    ) == expected


def test_scale_is_none_without_target_word_count() -> None:
    assert build_planning_scale(0, {"chapter_words": 2000}) is None
    assert build_planning_scale(None, {"chapter_words": 2000}) is None


def test_volume_chapter_range_keeps_legacy_behaviour() -> None:
    # 旧三键窗口内的小范围语义不变。
    assert resolve_volume_chapter_range(
        {"chapters_per_volume_min": 3, "chapters_per_volume_max": 4}
    ) == (3, 4)
    # 简写键是每卷章数的真值,不夹取。
    assert resolve_volume_chapter_range({"chapters_per_volume": 100}) == (100, 100)
    assert resolve_volume_chapter_range({}) is None


def test_volume_chapter_range_does_not_halve_a_canonical_100() -> None:
    """规范键写「每卷 100 章」时,卷纲硬约束必须是 100 章而不是被夹取成 50 章。

    旧实现让这一路返回 (50, 50):卷纲只产出 50 章,每章预算被摊成
    「卷目标字数 ÷ 50」(1M/5 卷 = 200000 → 4000 字/章),与已确认书纲的
    「每卷 100 章、每章 2000 字」自相矛盾。
    """
    assert resolve_chapters_per_volume_pref({"chapters_per_volume_min": 100}) == (50, 50)
    assert resolve_volume_chapter_range({"chapters_per_volume_min": 100}) == (100, 100)
    assert resolve_volume_chapter_range(
        {"chapters_per_volume_min": 100, "chapters_per_volume_max": 200}
    ) == (100, 200)
    # 上限仍存在,避免模型/设置给出荒谬的每卷章数。
    assert resolve_volume_chapter_range({"chapters_per_volume_min": 5_000}) == (200, 200)
