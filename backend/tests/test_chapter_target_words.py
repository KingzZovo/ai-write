from __future__ import annotations

from app.services.chapter_target_words import (
    CHAPTER_DEFAULT_WORD_COUNT,
    LEGACY_CHAPTER_DEFAULT_WORD_COUNT,
    is_legacy_chapter_target_word_count,
    resolve_chapter_target_word_count,
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
