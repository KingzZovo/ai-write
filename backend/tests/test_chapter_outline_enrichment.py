"""Regression: level=chapter outline generation must enrich chapters.outline_json.

Live gap (2026-06-29 全流程测试): generating a level=chapter outline wrote only
the `outlines` table and (optionally) a title back onto the Chapter row. It
NEVER updated `chapters.outline_json`, which the prose generator + readiness
gate actually read. So every chapter kept the thin volume-batch skeleton
({chapter_idx, title, summary, key_events}) and the richer per-chapter outline
had zero effect on prose. Worse, chapters looked inconsistent: the ones that
went through cascade during prose gen accreted extra debt keys, so ch1/ch3
showed 4 keys while ch2/ch4/ch5 showed 8 — not a real content difference.

`merge_chapter_outline_enrichment` splices the freshly generated per-chapter
content into the existing skeleton so prose reads the rich version, while
preserving the skeleton's authoritative chapter_idx and never regressing a
populated field back to empty.
"""
from __future__ import annotations

from app.services.outline_generator import merge_chapter_outline_enrichment


def test_enrichment_fills_thin_skeleton():
    skeleton = {
        "chapter_idx": 3,
        "title": "吞声之水",
        "summary": "短摘要。",
        "key_events": ["a", "b"],
    }
    generated = {
        "summary": "一段远比骨架丰富的章节梗概，交代场景、冲突、转折与钩子。" * 3,
        "key_events": ["事件1", "事件2", "事件3", "事件4"],
        "scene_beats": [{"beat": "开场"}, {"beat": "对峙"}, {"beat": "反转"}],
        "characters": ["江临", "顾远"],
        "raw_text": "should not clobber structured fields",
    }
    merged = merge_chapter_outline_enrichment(skeleton, generated)

    # chapter_idx from the skeleton is authoritative (never taken from LLM output).
    assert merged["chapter_idx"] == 3
    # Rich generated fields win over the thin skeleton.
    assert merged["summary"] == generated["summary"]
    assert merged["key_events"] == generated["key_events"]
    # New structured fields are added.
    assert merged["scene_beats"] == generated["scene_beats"]
    assert merged["characters"] == generated["characters"]


def test_enrichment_never_regresses_to_empty():
    # If the generated content is thinner/empty on a field, keep the skeleton's.
    skeleton = {
        "chapter_idx": 1,
        "title": "归来之人",
        "summary": "已有的可用摘要。",
        "key_events": ["x", "y"],
    }
    generated = {
        "summary": "",          # empty -> must not overwrite
        "key_events": [],       # empty -> must not overwrite
        "scene_beats": [{"beat": "新增"}],
    }
    merged = merge_chapter_outline_enrichment(skeleton, generated)
    assert merged["summary"] == "已有的可用摘要。"
    assert merged["key_events"] == ["x", "y"]
    assert merged["scene_beats"] == [{"beat": "新增"}]
    assert merged["title"] == "归来之人"


def test_enrichment_preserves_chapter_idx_even_if_generated_differs():
    skeleton = {"chapter_idx": 5, "title": "第5章", "summary": "s"}
    generated = {"chapter_idx": 99, "summary": "richer summary here, quite long."}
    merged = merge_chapter_outline_enrichment(skeleton, generated)
    assert merged["chapter_idx"] == 5  # skeleton wins, not 99


def test_enrichment_drops_parse_error_and_debt_noise():
    # A parse-failed generated payload must not poison the skeleton.
    skeleton = {"chapter_idx": 2, "title": "碎裂无声", "summary": "ok", "key_events": ["e"]}
    generated = {"_parse_error": True, "raw_text": "garbage that failed to parse"}
    merged = merge_chapter_outline_enrichment(skeleton, generated)
    # Nothing usable in generated -> skeleton preserved unchanged.
    assert merged["summary"] == "ok"
    assert merged["key_events"] == ["e"]
    assert "_parse_error" not in merged
