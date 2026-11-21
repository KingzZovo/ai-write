"""Tests for title_quality_checker (PR-TITLE-Q1 / Q1.1).

Locks the static title-quality rules in CI so future edits to the rule sets
or thresholds cannot silently regress. These tests cover ONLY the pure
rule-detection layer (`_violations_for` + `check_titles`); the LLM-backed
rewrite path (`rewrite_titles`, `check_and_rewrite_in_place`) is exercised
by integration tests against the volume-outline generator.

User-facing intent locked here:
  - Object nouns cannot do abstract conscious verbs (钾认账 / 灯还债 …).
  - 2nd-person `你` is allowed in dialog/POV titles, blocked only when it
    addresses the reader as meta-narration (`你应该知道的…`).
  - PR-TITLE-Q1.1: title length is soft (字数不齐 OK), only a hard ceiling
    of 14 Chinese chars / 18 raw chars (catches a paragraph leaking into
    the title slot).
  - Hard bans stay: `第N章` placeholder, pure digits, full-width Chinese
    colon, modern/engineering vocab, pure-abstract empty words.

If any of these assertions break, the rule was changed; either update the
test deliberately (with a doc note) or revert the rule change.
"""
from __future__ import annotations

import pytest

from app.services.title_quality_checker import (
    _violations_for,
    check_titles,
)


# --------------------------------------------------------------------- #
# Clean titles — must produce zero violations.
# --------------------------------------------------------------------- #

CLEAN_TITLES = [
    # Concrete event titles from 赤心 vol1 (already shipped clean).
    "义庄夜收无名尸",
    "按印者欠命",
    "夜更三十三响",
    "认尸者无门",
    "峁贡棺中有故人",
    # 2nd-person used as POV/dialog — SHOULD pass (not meta-address).
    "你别回头",
    "你欠的那笔债",
    # Slightly longer but still clean (within 14 Chinese-char ceiling).
    "万人命债倒灌护山阵",  # 9 chars
    "吞篠补窍失一段记忆",  # 9 chars (chekhov-style)
]


@pytest.mark.parametrize("title", CLEAN_TITLES)
def test_clean_titles_have_no_violations(title: str) -> None:
    assert _violations_for(title) == [], f"unexpected violations for {title!r}"


# --------------------------------------------------------------------- #
# Each rule — one positive case per reason code.
# --------------------------------------------------------------------- #

RULE_POSITIVE_CASES: list[tuple[str, str]] = [
    ("第三章", "placeholder_chapter_n"),
    ("12345", "pure_digits"),
    ("陈计：夜过三更", "chinese_colon"),
    ("依靠 SOP 守山门", "modern_term"),
    ("虚无", "abstract_empty"),
    ("灯还债", "object_abstract_verb"),
    ("你应该知道的那件事", "2p_meta_address"),
    # too_long: paragraph leaking into the title slot. >14 Chinese chars.
    (
        "陈计在义庄中收下一具无名尸并让温幼二看守过夜",  # 21 chars
        "too_long",
    ),
    ("一", "too_short"),  # 1 char only.
]


@pytest.mark.parametrize("title,reason", RULE_POSITIVE_CASES)
def test_each_rule_fires(title: str, reason: str) -> None:
    reasons = _violations_for(title)
    assert reason in reasons, (
        f"expected reason {reason!r} for title {title!r}, got {reasons!r}"
    )


# --------------------------------------------------------------------- #
# 2nd-person allow-list — must NOT fire on legit POV/dialog titles.
# --------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "title",
    [
        "你别回头",
        "你欠的那笔债",
        "你看这灯",
    ],
)
def test_2p_pov_titles_pass(title: str) -> None:
    assert "2p_meta_address" not in _violations_for(title)


# --------------------------------------------------------------------- #
# Length ceiling — boundary cases for PR-TITLE-Q1.1 (字数不齐 OK).
# --------------------------------------------------------------------- #

def test_length_ceiling_14_chinese_chars_passes() -> None:
    # Exactly 14 Chinese chars: at the ceiling, MUST pass.
    title = "陈计在义庄中收下一具无名尸体"  # 14 chars
    assert len(title) == 14
    assert "too_long" not in _violations_for(title)


def test_length_ceiling_15_chinese_chars_fires() -> None:
    # 15 Chinese chars: just over ceiling, MUST trip too_long.
    title = "陈计在义庄中收下一具无名尸体了"  # 15 chars
    assert len(title) == 15
    assert "too_long" in _violations_for(title)


def test_short_titles_remain_legal() -> None:
    # 2-3 char titles like '义庄' / '按印' must stay legal
    # (PR-TITLE-Q1.1 explicitly allows uneven lengths).
    for t in ["义庄", "按印", "夜更"]:
        assert _violations_for(t) == []


# --------------------------------------------------------------------- #
# check_titles aggregator — only flags violators and preserves indices.
# --------------------------------------------------------------------- #

def test_check_titles_filters_only_violators() -> None:
    summaries = [
        {"chapter_idx": 1, "title": "义庄夜收无名尸", "summary": "...", "key_events": []},
        {"chapter_idx": 2, "title": "第二章", "summary": "placeholder", "key_events": []},
        {"chapter_idx": 3, "title": "灯还债", "summary": "object verb", "key_events": []},
    ]
    out = check_titles(summaries)
    flagged_idx = {v["chapter_idx"] for v in out}
    assert flagged_idx == {2, 3}
    by_idx = {v["chapter_idx"]: v for v in out}
    assert "placeholder_chapter_n" in by_idx[2]["reasons"]
    assert "object_abstract_verb" in by_idx[3]["reasons"]


def test_check_titles_skips_non_dict_entries() -> None:
    out = check_titles([None, "raw string", 42, {"chapter_idx": 1, "title": "灯还债"}])
    assert len(out) == 1
    assert out[0]["chapter_idx"] == 1


def test_check_titles_handles_empty_input() -> None:
    assert check_titles([]) == []
    assert check_titles(None) == []  # type: ignore[arg-type]
