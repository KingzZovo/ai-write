"""Regression tests for v1.3.0 chunk-30 regenerate-volume budget auto-wiring.

These are intentionally pure algorithm tests that exercise the exact shape
used inside ``backend/app/api/volumes.py::regenerate_volume`` after chapters
are re-created from a fresh volume outline, without standing up FastAPI,
the DB session, or the LLM.

Contract being pinned:
- A regenerated volume must allocate ``volume.target_word_count`` across
  its newly-created chapters using ``allocate_even``.
- The resulting sum must equal ``volume.target_word_count`` exactly
  (remainder absorbed by the last chapter).
- With zero chapters the allocator must return an empty list and leave the
  volume target untouched.
"""

from __future__ import annotations

from app.services.budget_allocator import (
    CHAPTER_DEFAULT,
    VOLUME_DEFAULT,
    allocate_even,
)


class _FakeChapter:
    """Stand-in for the ORM Chapter row used by regenerate_volume."""

    def __init__(self, chapter_idx: int) -> None:
        self.chapter_idx = chapter_idx
        self.target_word_count = CHAPTER_DEFAULT  # DB default


def _apply_regenerate_flow(volume_target: int, n_chapters: int) -> list[_FakeChapter]:
    """Mirror the exact code path inside regenerate_volume."""
    chapters = [_FakeChapter(i + 1) for i in range(n_chapters)]
    if chapters and volume_target > 0:
        alloc = allocate_even(volume_target, len(chapters))
        for ch, wc in zip(chapters, alloc):
            ch.target_word_count = int(wc)
    return chapters


def test_regenerate_5_chapters_default_volume():
    chapters = _apply_regenerate_flow(VOLUME_DEFAULT, 5)
    targets = [c.target_word_count for c in chapters]
    assert targets == [40_000, 40_000, 40_000, 40_000, 40_000]
    assert sum(targets) == VOLUME_DEFAULT


def test_regenerate_remainder_absorbed_by_last_chapter():
    chapters = _apply_regenerate_flow(100_001, 3)
    targets = [c.target_word_count for c in chapters]
    assert targets == [33_333, 33_333, 33_335]
    assert sum(targets) == 100_001


def test_regenerate_single_chapter_gets_full_budget():
    chapters = _apply_regenerate_flow(VOLUME_DEFAULT, 1)
    assert chapters[0].target_word_count == VOLUME_DEFAULT


def test_regenerate_zero_chapters_is_noop():
    # No exception, no allocation, no leftover side effects.
    chapters = _apply_regenerate_flow(VOLUME_DEFAULT, 0)
    assert chapters == []


def test_regenerate_volume_target_zero_leaves_chapters_at_default():
    # If a project/volume somehow has target 0 we must not wipe chapter defaults.
    chapters = _apply_regenerate_flow(0, 3)
    for c in chapters:
        assert c.target_word_count == CHAPTER_DEFAULT


def test_regenerate_large_even_split_30_chapters():
    # Realistic 30-chapter volume totaling 600_000 -> 20_000 each, zero remainder.
    chapters = _apply_regenerate_flow(600_000, 30)
    targets = [c.target_word_count for c in chapters]
    assert all(t == 20_000 for t in targets)
    assert sum(targets) == 600_000


def test_regenerate_overwrites_default_chapter_value():
    # After regenerate, every chapter must have left the 50_000 default;
    # assert via the first assertion on sum then via distinctness.
    chapters = _apply_regenerate_flow(210_000, 3)
    targets = [c.target_word_count for c in chapters]
    assert sum(targets) == 210_000
    # 210_000 / 3 == 70_000 for every chapter, which is != CHAPTER_DEFAULT.
    assert all(t != CHAPTER_DEFAULT for t in targets)
