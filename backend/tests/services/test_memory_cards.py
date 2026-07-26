"""Tier-2 memory cards (定位层) — deterministic excerpt extraction tests.

Covers the pure planning helpers in app.tasks.entity_tasks:
- same text → same excerpts (determinism / idempotent re-run)
- sentence-bounded windows, <=300 chars, containing the name
- first_appearance only on the character's first-seen chapter
- retention planning (cap, first_appearance always kept, oldest key_moments
  evicted first)
and the DB wiring of ``_upsert_memory_cards`` (upsert + eviction + commit).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tasks.entity_tasks import (
    _MEMORY_CARD_MAX_CHARS,
    _sentence_bounded_excerpt,
    _upsert_memory_cards,
    plan_card_eviction,
    plan_memory_cards,
)

_SENTENCE_ENDERS = "。！？…!?\n"

# Long enough (~900 chars) that the first-occurrence window and the
# midpoint-occurrence window are distinct 300-char excerpts.
_FILLER_A = "山道上的风一阵紧过一阵，吹得檐角的铜铃叮当作响。" * 9
_FILLER_B = "炉膛里的柴火噼啪炸开，火星子溅到青砖地上又灭了。" * 12
_FILLER_C = "窗纸被雨点打得沙沙作响，烛火跟着晃了两晃。" * 9
_CHAPTER = (
    _FILLER_A
    + "林岚推门进来，肩头全湿了。他把伞靠在墙角，没有说话。"
    + _FILLER_B
    + "许久之后，林岚才开口：这一趟，白跑了。"
    + _FILLER_C
    + "苏眉从里屋出来，端着一碗热汤。她把汤放在桌上，看了他一眼。"
)


class TestSentenceBoundedExcerpt:
    def test_excerpt_contains_name_and_respects_bounds(self):
        pos = _CHAPTER.find("林岚")
        excerpt = _sentence_bounded_excerpt(_CHAPTER, pos, 2)
        assert "林岚" in excerpt
        assert len(excerpt) <= _MEMORY_CARD_MAX_CHARS
        # Sentence-bounded: the window starts at a sentence start and ends
        # on a sentence terminator (or text end).
        start = _CHAPTER.find(excerpt)
        assert start >= 0
        assert start == 0 or _CHAPTER[start - 1] in _SENTENCE_ENDERS
        end = start + len(excerpt)
        assert end == len(_CHAPTER) or excerpt[-1] in _SENTENCE_ENDERS

    def test_long_sentence_hard_trims_around_occurrence(self):
        text = "废" * 500 + "林岚" + "话" * 500  # no sentence enders at all
        pos = text.find("林岚")
        excerpt = _sentence_bounded_excerpt(text, pos, 2)
        assert len(excerpt) == _MEMORY_CARD_MAX_CHARS
        assert "林岚" in excerpt

    def test_deterministic(self):
        pos = _CHAPTER.find("苏眉")
        assert _sentence_bounded_excerpt(_CHAPTER, pos, 2) == (
            _sentence_bounded_excerpt(_CHAPTER, pos, 2)
        )


class TestPlanMemoryCards:
    def test_same_text_same_plan(self):
        args = (_CHAPTER, 7, ["林岚", "苏眉"], {})
        assert plan_memory_cards(*args) == plan_memory_cards(*args)

    def test_first_appearance_when_no_roster_entry(self):
        rows = plan_memory_cards(_CHAPTER, 7, ["林岚"], {})
        types = [r["card_type"] for r in rows]
        assert "first_appearance" in types
        fa = next(r for r in rows if r["card_type"] == "first_appearance")
        assert fa["character_name"] == "林岚"
        assert fa["global_idx"] == 7
        assert "林岚" in fa["excerpt"]

    def test_first_appearance_when_roster_first_seen_is_this_chapter(self):
        rows = plan_memory_cards(_CHAPTER, 7, ["林岚"], {"林岚": 7})
        assert any(r["card_type"] == "first_appearance" for r in rows)

    def test_key_moment_only_when_seen_earlier(self):
        rows = plan_memory_cards(_CHAPTER, 7, ["林岚"], {"林岚": 3})
        assert [r["card_type"] for r in rows] == ["key_moment"]
        # Midpoint occurrence: the SECOND 林岚 occurrence is nearer the
        # chapter's midpoint than the first one.
        midpoint = len(_CHAPTER) // 2
        occ2 = _CHAPTER.find("林岚", _CHAPTER.find("林岚") + 2)
        assert abs(occ2 - midpoint) < abs(_CHAPTER.find("林岚") - midpoint)
        assert "白跑了" in rows[0]["excerpt"]

    def test_single_occurrence_first_chapter_yields_one_card(self):
        rows = plan_memory_cards(_CHAPTER, 7, ["苏眉"], {})
        # 苏眉 appears once → first and midpoint windows coincide → no
        # duplicate key_moment.
        assert [r["card_type"] for r in rows] == ["first_appearance"]

    def test_absent_and_short_names_skipped(self):
        rows = plan_memory_cards(_CHAPTER, 7, ["王五", "雨"], {})
        assert rows == []

    def test_empty_text_yields_nothing(self):
        assert plan_memory_cards("", 7, ["林岚"], {}) == []


class TestPlanCardEviction:
    def test_under_cap_no_eviction(self):
        cards = [("a", "first_appearance", 1), ("b", "key_moment", 2)]
        assert plan_card_eviction(cards, 10) == []

    def test_evicts_oldest_key_moments_keeps_first_appearance(self):
        cards = [("fa", "first_appearance", 1)] + [
            (f"km{i}", "key_moment", i) for i in range(2, 14)
        ]  # 1 + 12 = 13 cards, cap 10 → evict 3 oldest key_moments
        doomed = plan_card_eviction(cards, 10)
        assert doomed == ["km2", "km3", "km4"]
        assert "fa" not in doomed

    def test_first_appearance_never_evicted_even_over_cap(self):
        cards = [(f"fa{i}", "first_appearance", i) for i in range(5)] + [
            ("km", "key_moment", 9)
        ]
        doomed = plan_card_eviction(cards, 3)
        assert doomed == ["km"]

    def test_idempotent_convergence(self):
        cards = [("fa", "first_appearance", 1)] + [
            (f"km{i}", "key_moment", i) for i in range(2, 14)
        ]
        doomed = set(plan_card_eviction(cards, 10))
        survivors = [c for c in cards if c[0] not in doomed]
        assert len(survivors) == 10
        # Re-running eviction over the surviving set is a no-op.
        assert plan_card_eviction(survivors, 10) == []


class _SessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_upsert_memory_cards_upserts_and_evicts(monkeypatch):
    """DB wiring: plan → ON CONFLICT upsert → per-character eviction → commit."""
    db = AsyncMock()

    names_result = MagicMock()
    names_result.all.return_value = [("林岚",)]
    roster_result = MagicMock()
    roster_result.all.return_value = []  # no roster → this is the first chapter

    # 林岚 in _CHAPTER on a first-seen chapter → 2 planned rows
    # (first_appearance + key_moment) → 2 insert executes.
    cards_result = MagicMock()
    cards_result.all.return_value = [("fa", "first_appearance", 1)] + [
        (f"km{i}", "key_moment", i) for i in range(2, 14)
    ]  # 13 cards → cap 10 → delete 3

    delete_result = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            names_result,   # character names
            roster_result,  # roster first_seen
            MagicMock(),    # insert first_appearance
            MagicMock(),    # insert key_moment
            cards_result,   # retention select
            delete_result,  # eviction delete
        ]
    )
    db.commit = AsyncMock()
    monkeypatch.setattr(
        "app.db.session.async_session_factory", lambda: _SessionCtx(db)
    )

    out = await _upsert_memory_cards(
        project_id="pid-1", global_idx=7, chapter_text=_CHAPTER
    )

    assert out == {"cards_planned": 2, "cards_evicted": 3}
    assert db.execute.await_count == 6
    db.commit.assert_awaited_once()
    # The two planned-row statements are PG inserts with the unique-key
    # ON CONFLICT DO UPDATE (idempotent re-extraction overwrites).
    insert_stmts = [db.execute.await_args_list[i].args[0] for i in (2, 3)]
    for stmt in insert_stmts:
        assert stmt.table.name == "character_memory_cards"
        assert stmt._post_values_clause is not None  # on_conflict_do_update
