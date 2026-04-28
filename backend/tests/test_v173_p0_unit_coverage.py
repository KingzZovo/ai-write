"""v1.7.3 Step 2 — P0 unit-test gap fill for low-coverage services.

Targets (per docs/AUDIT_BASELINE_v1.7.2.md):
  style_abstractor (0%), beat_extractor (0%), feature_extractor (41%),
  chapter_generator (42%), outline_generator (13%).

Unit-level only: no DB / Qdrant / real LLM. We mock prompt-registry calls
for the wrapper services and exercise pure helpers directly.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import beat_extractor, style_abstractor
from app.services.chapter_generator import ChapterGenerator
from app.services.feature_extractor import (
    PlotFeatures,
    StyleExtractor,
    StyleFeatures,
    _parse_json,
)
from app.services.outline_generator import OutlineGenerator


# --- style_abstractor.abstract_style ---------------------------------------

def test_style_abstractor_empty_returns_empty():
    assert asyncio.run(style_abstractor.abstract_style("", db=None)) == {}
    assert asyncio.run(style_abstractor.abstract_style("  \n\t ", db=None)) == {}


def test_style_abstractor_success_returns_payload():
    payload = {"sentence_avg": 12.0, "pov": "third_person"}
    with patch(
        "app.services.style_abstractor.run_structured_prompt",
        new=AsyncMock(return_value=payload),
    ):
        out = asyncio.run(style_abstractor.abstract_style("text", db=None))
    assert out == payload


def test_style_abstractor_parse_error_returns_empty():
    with patch(
        "app.services.style_abstractor.run_structured_prompt",
        new=AsyncMock(return_value={"parse_error": True}),
    ):
        assert asyncio.run(style_abstractor.abstract_style("text", db=None)) == {}


def test_style_abstractor_exception_swallowed():
    with patch(
        "app.services.style_abstractor.run_structured_prompt",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert asyncio.run(style_abstractor.abstract_style("text", db=None)) == {}


# --- beat_extractor.extract_beat -------------------------------------------

def test_beat_extractor_empty_returns_empty():
    assert asyncio.run(beat_extractor.extract_beat("", db=None)) == {}
    assert asyncio.run(beat_extractor.extract_beat("   ", db=None)) == {}


def test_beat_extractor_flat_dict_passthrough():
    flat = {"scene_type": "action", "subject": "X"}
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(return_value=flat),
    ):
        assert asyncio.run(beat_extractor.extract_beat("raw", db=None)) == flat


def test_beat_extractor_beats_wrapper_collapses_to_first():
    wrapped = {"beats": [{"scene_type": "reveal"}, {"scene_type": "action"}]}
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(return_value=wrapped),
    ):
        out = asyncio.run(beat_extractor.extract_beat("raw", db=None))
    assert out == {"scene_type": "reveal"}


def test_beat_extractor_items_wrapper_collapses_to_first():
    wrapped = {"items": [{"scene_type": "setup", "subject": "A"}]}
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(return_value=wrapped),
    ):
        out = asyncio.run(beat_extractor.extract_beat("raw", db=None))
    assert out == {"scene_type": "setup", "subject": "A"}


def test_beat_extractor_non_dict_array_element_returns_empty():
    wrapped = {"beats": ["oops-this-is-a-string"]}
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(return_value=wrapped),
    ):
        assert asyncio.run(beat_extractor.extract_beat("raw", db=None)) == {}


def test_beat_extractor_parse_error_returns_empty():
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(return_value={"parse_error": True}),
    ):
        assert asyncio.run(beat_extractor.extract_beat("raw", db=None)) == {}


def test_beat_extractor_exception_swallowed():
    with patch(
        "app.services.beat_extractor.run_structured_prompt",
        new=AsyncMock(side_effect=ValueError("nope")),
    ):
        assert asyncio.run(beat_extractor.extract_beat("raw", db=None)) == {}


# --- feature_extractor pure helpers ----------------------------------------

def test_plotfeatures_to_dict():
    d = PlotFeatures(
        characters=["A"], events=["e"], summary="s",
        emotional_tone="warm", locations=["city"], causal_chains=["x->y"],
    ).to_dict()
    assert d == {
        "characters": ["A"], "events": ["e"], "summary": "s",
        "emotional_tone": "warm", "locations": ["city"],
        "causal_chains": ["x->y"],
    }


def test_stylefeatures_default_to_dict():
    d = StyleFeatures().to_dict()
    assert d["avg_sentence_length"] == 0.0
    assert d["top_words"] == []
    assert d["rhetoric_frequency"] == {}
    assert d["pov_type"] == ""


def test_parse_json_plain():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_strips_markdown_fence():
    raw = '```json\n{"a": 2, "b": [1, 2]}\n```'
    assert _parse_json(raw) == {"a": 2, "b": [1, 2]}


def test_parse_json_strips_bare_fence():
    raw = '```\n{"k": "v"}\n```'
    assert _parse_json(raw) == {"k": "v"}


def test_parse_json_invalid_raises():
    with pytest.raises(Exception):
        _parse_json("not json at all")


def test_style_extractor_empty_returns_default():
    feats = StyleExtractor().extract("")
    assert feats.avg_sentence_length == 0.0
    assert feats.pov_type == ""


def test_style_extractor_first_person_pov():
    text = (
        "\u6211\u8d70\u8fdb\u4e86\u90a3\u4e2a\u623f\u95f4\u3002\n"
        "\u6211\u770b\u5230\u4e86\u4e00\u62b9\u9633\u5149\u3002\n"
        "\u6211\u62ac\u8d77\u5934\u770b\u90a3\u4e2a\u4eba\u3002"
    )
    feats = StyleExtractor().extract(text)
    assert feats.pov_type == "first_person"
    assert feats.avg_sentence_length > 0
    assert set(feats.rhetoric_frequency.keys()) == {"simile", "parallelism", "rhetorical_question"}
    assert all(t in {"S", "M", "L"} for t in feats.paragraph_rhythm.split("-"))


def test_style_extractor_dialogue_ratio_positive():
    text = '\u4ed6\u8bf4\uff1a\u201c\u4f60\u597d\u5417\uff1f\u201d\u5979\u5fae\u7b11\u3002'
    feats = StyleExtractor().extract(text)
    assert feats.dialogue_ratio > 0.0
    assert 0.0 <= feats.narration_ratio <= 1.0


# --- chapter_generator._collect_rag_hits -----------------------------------

def test_collect_rag_hits_flattens_layers():
    pack = SimpleNamespace(
        rag_snippets=["snip-a", "snip-b"],
        dialogue_samples={"Alice": ["hi"], "Bob": ["yo"]},
        style_samples=["style-1"],
    )
    hits = ChapterGenerator._collect_rag_hits(pack)
    cols = [h["collection"] for h in hits]
    assert cols.count("chapter_summaries") == 2
    assert cols.count("dialogue_samples") == 2
    assert cols.count("style_samples") == 1
    assert {h["payload"]["summary"] for h in hits if h["collection"] == "chapter_summaries"} == {"snip-a", "snip-b"}
    alice = next(h for h in hits if h["collection"] == "dialogue_samples" and h["payload"]["character"] == "Alice")
    assert alice["payload"]["lines"] == ["hi"]


def test_collect_rag_hits_empty_pack():
    pack = SimpleNamespace(rag_snippets=[], dialogue_samples={}, style_samples=[])
    assert ChapterGenerator._collect_rag_hits(pack) == []


# --- outline_generator pure helpers ----------------------------------------

def _gen() -> OutlineGenerator:
    return OutlineGenerator()


def test_iter_sections_yields_in_document_order():
    text = "\u4e00\u3001\u9996\u90e8\u5206\nfoo\n\u4e8c\u3001\u6b21\u90e8\u5206\nbar"
    out = list(_gen()._iter_sections(text))
    assert [n for n, _ in out] == ["\u4e00", "\u4e8c"]
    assert "foo" in out[0][1]
    assert "bar" in out[1][1]


def test_iter_sections_empty_text():
    assert list(_gen()._iter_sections("")) == []


def test_reassemble_sections_first_occurrence_wins_canonical_order():
    skeleton = "\u4e00\u3001A\nbook\n\u4e09\u3001C\nability\n"
    chars = "\u4e8c\u3001B\nroles\n\u4e09\u3001C-dup\nshould-not-win\n"
    world = "\u516d\u3001F\nworld\n"
    out = _gen()._reassemble_sections(skeleton, chars, world)
    # Order must be 一,二,三,六 (no 四/五/七/八/九 supplied)
    pos = lambda needle: out.index(needle)  # noqa: E731
    assert pos("\u4e00\u3001A") < pos("\u4e8c\u3001B") < pos("\u4e09\u3001C") < pos("\u516d\u3001F")
    # First-wins: "should-not-win" body must NOT appear
    assert "should-not-win" not in out
    assert "ability" in out


def test_outline_parse_json_plain():
    assert _gen()._parse_json('{"a": 1}') == {"a": 1}


def test_outline_parse_json_with_markdown_fence():
    raw = '```json\n{"x": [1, 2]}\n```'
    assert _gen()._parse_json(raw) == {"x": [1, 2]}


def test_outline_parse_json_invalid_returns_raw_text_marker():
    out = _gen()._parse_json("this is not json")
    assert out["_parse_error"] is True
    assert out["raw_text"] == "this is not json"
