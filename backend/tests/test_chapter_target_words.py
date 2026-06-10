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
