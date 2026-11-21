from __future__ import annotations

from typing import Any

CHAPTER_DEFAULT_WORD_COUNT = 4000
LEGACY_CHAPTER_DEFAULT_WORD_COUNT = 50_000


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_legacy_chapter_target_word_count(value: Any) -> bool:
    return _positive_int(value) == LEGACY_CHAPTER_DEFAULT_WORD_COUNT


def resolve_chapter_target_word_count(
    chapter_target_word_count: Any,
    project_target_chapter_words: Any = None,
    *,
    fallback: int = CHAPTER_DEFAULT_WORD_COUNT,
) -> int:
    """Return the effective target used for generation and UI display.

    50_000 was the previous DB default, not a deliberate user preference in
    most rows. Treat that exact value as legacy when resolving targets so old
    rows inherit the project/default chapter size instead of forcing filler.
    """
    chapter_target = _positive_int(chapter_target_word_count)
    if chapter_target and chapter_target != LEGACY_CHAPTER_DEFAULT_WORD_COUNT:
        return chapter_target

    project_target = _positive_int(project_target_chapter_words)
    if project_target:
        return project_target

    fallback_target = _positive_int(fallback)
    return fallback_target or CHAPTER_DEFAULT_WORD_COUNT
