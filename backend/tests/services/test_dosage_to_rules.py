"""Tests for dosage_to_rules deriver — deterministic golden rules.

The deriver is pure-Python and side-effect-free, so we can run it in a
plain unit test (no DB, no LLM). The fixture below mirrors the v8 龙族
dosage_profile shape; assertions lock how each metric is rendered to a
rule so we catch unintended drift in the prompt-facing text.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Resolve the backend/app package without an installed package.
_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[2]
sys.path.insert(0, str(_BACKEND))

from app.services.dosage_to_rules import (  # noqa: E402
    derive_rules_from_dosage,
    merge_anti_ai_rules,
)


LONGZU_FIXTURE: dict = {
    "sentence": {"count": 62195, "std_chars": 22.53, "mean_chars": 28.61},
    "paragraph": {"count": 28220, "std_chars": 58.02, "mean_chars": 63.06},
    "dialogue": {
        "ratio": 0.3377,
        "per_kchar": 12.3099,
        "turn_count": 21907,
        "turn_chars_mean": 27.43,
    },
    "metaphor": {
        "total_per_kchar": 4.4054,
        "sentence_end_per_kchar": 1.4964,
        "patterns_per_kchar": {
            "像一": 0.1202,
            "像被": 0.0202,
            "像有人": 0.0056,
            "像某种": 0.0,
            "像随时": 0.0022,
        },
    },
    "psychology": {
        "pattern_total_per_kchar": 0.0315,
        "neutral_words_per_kchar": 4.216,
        "pattern_per_chapter_7k": 0.22,
        "patterns_per_kchar": {
            "咬牙": 0.0017,
            "身体X": 0.0,
            "脸色X": 0.0017,
            "喉结动": 0.0,
            "握紧拳": 0.0034,
            "呼吸一X": 0.0,
            "喉咙发X": 0.0,
            "心里一X": 0.0017,
            "眼皮一X": 0.0,
            "头皮发麻": 0.0067,
            "指节发白": 0.0006,
            "眼神眼底": 0.0,
            "深吸一口气": 0.0157,
        },
    },
    "parallelism": {"XYX_per_kchar": 0.0517, "ABAB_per_kchar": 0.4035},
    "colloquial": {
        "particles_per_kchar": 0.7715,
        "onomatopoeia_per_kchar": 0.1141,
    },
    "total_chars": 1779631,
    "total_kchars": 1779.6,
    "ai_metawords_count": {
        "prompt": 0,
        "五感": 3,
        "伏笔": 0,
        "短句": 0,
        "节奏": 38,
        "节拍": 6,
        "转折": 16,
        "钩子": 1,
        "以下是": 1,
        "根据您": 2,
        "黑名单": 1,
    },
}


def test_longzu_dosage_emits_expected_rule_count() -> None:
    rules, anti_ai = derive_rules_from_dosage(LONGZU_FIXTURE, profile_version="v8.0")
    # Sanity: at least one rule per dimension that has data
    assert len(rules) >= 12, f"expected >=12 rules, got {len(rules)}"
    # Anti-AI metaword guards: prompt-leak (黑名单/以下是/根据您) +
    # meta-narration (节奏/节拍/转折/钩子/五感) = 8 entries.
    assert len(anti_ai) >= 8, f"expected >=8 anti-ai, got {len(anti_ai)}"


def test_longzu_dosage_emits_signature_rules() -> None:
    rules, _ = derive_rules_from_dosage(LONGZU_FIXTURE)
    rule_text = " | ".join(r["rule"] for r in rules)
    # The dialogue ratio (33.8%) and the top metaphor patterns must be in
    # the rendered rules; these are the dosage profile's signatures.
    assert "33.8%" in rule_text
    assert "「像一」" in rule_text
    assert "深吸一口气" in rule_text  # top psychology pattern
    assert "ABAB" in rule_text


def test_categories_match_existing_compiler_buckets() -> None:
    rules, _ = derive_rules_from_dosage(LONGZU_FIXTURE)
    # style_compiler.compile_style only filters category=="structure"; every
    # rule we emit MUST not be in that bucket so they all get injected.
    cats = {r["category"] for r in rules}
    assert "structure" not in cats
    assert cats <= {"rhythm", "dialogue", "description", "style", "custom"}


def test_weights_are_in_expected_tiers() -> None:
    rules, _ = derive_rules_from_dosage(LONGZU_FIXTURE)
    for r in rules:
        assert 0.0 < r["weight"] <= 0.95, r
        # Weights snap to one of the tier values returned by _weight_for.
        assert r["weight"] in (0.55, 0.65, 0.70, 0.85)


def test_each_rule_has_source_metric() -> None:
    rules, _ = derive_rules_from_dosage(LONGZU_FIXTURE)
    for r in rules:
        assert r.get("source_metric"), f"missing source_metric: {r}"


def test_deterministic_byte_stable() -> None:
    a, ax = derive_rules_from_dosage(LONGZU_FIXTURE, profile_version="v8.0")
    b, bx = derive_rules_from_dosage(LONGZU_FIXTURE, profile_version="v8.0")
    assert a == b
    assert ax == bx


def test_empty_dosage_returns_empty() -> None:
    rules, anti_ai = derive_rules_from_dosage({})
    assert rules == []
    assert anti_ai == []
    rules, anti_ai = derive_rules_from_dosage(None)  # type: ignore[arg-type]
    assert rules == []
    assert anti_ai == []


def test_partial_dosage_degrades_gracefully() -> None:
    rules, _ = derive_rules_from_dosage({"dialogue": {"ratio": 0.5}})
    # Only the dialogue.ratio branch fires (turn_chars_mean missing).
    assert len(rules) == 1
    assert rules[0]["category"] == "dialogue"
    assert "50.0%" in rules[0]["rule"]


def test_merge_anti_ai_rules_dedupes_by_pattern() -> None:
    existing = [
        {"pattern": "以下是", "replacement": "", "autoRewrite": False},
        {"pattern": "根据您", "replacement": "", "autoRewrite": False},
    ]
    additions = [
        {"pattern": "以下是", "reason": "prompt format leakage"},
        {"pattern": "黑名单", "reason": "prompt self-reference"},
    ]
    merged = merge_anti_ai_rules(existing, additions)
    patterns = [m["pattern"] for m in merged]
    assert patterns == ["以下是", "根据您", "黑名单"]
    # First entry preserved unchanged (existing wins on conflict).
    assert "reason" not in merged[0]
