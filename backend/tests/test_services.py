"""Unit tests for core services (no LLM/DB required).

Kept aligned with current implementation:
  - detect_style_features no longer emits `top_words` (was removed when the
    detector switched to `style_labels` + `ai_markers`).
  - compile_style emits structured sections ("写作风格参考", "【Anti-AI 规则】",
    "【风格关键词】") rather than a literal weight tier label.
A6 fix-up.
"""

import pytest

from app.services.quality_scorer import _parse_json
from app.services.style_compiler import compile_style
from app.services.style_detection import detect_style_features, features_to_rules


def test_style_detection_basic():
    text = "他走在雨中的街道上。路灯昏黄的光映在湿漉漉的柏油路面。" * 20
    features = detect_style_features(text)
    # Core keys the rest of the pipeline (features_to_rules, qdrant_store)
    # depends on. Guards against silent contract drift.
    assert "basic" in features
    assert features["basic"]["total_chars"] > 0
    assert "sentence_distribution" in features
    assert "dialogue_ratio" in features
    assert "description_density" in features
    assert "ai_markers" in features
    assert isinstance(features["ai_markers"], list)
    assert "style_labels" in features


def test_style_detection_handles_names_text():
    # Smoke test: detection must not crash on name-heavy input and must
    # still return the contract keys. Name filtering is now handled inside
    # the LLM-derived rules path (features_to_rules with llm_analysis), not
    # in the statistical features dict.
    text = "张三走在路上。李四在旁边说话。王五跑过来了。他们三人一起前行。" * 30
    features = detect_style_features(text)
    assert "basic" in features
    assert "sentence_distribution" in features
    assert "ai_markers" in features


def test_features_to_rules():
    features = {
        "sentence_distribution": {"short_pct": 0.5, "medium_pct": 0.3, "long_pct": 0.2},
        "dialogue_ratio": 0.4,
        "description_density": 2.0,
        "ai_markers": ["璜璨(3)", "油然而生(1)"],
    }
    rules, anti_ai = features_to_rules(features)
    assert len(rules) > 0
    # Statistical path must produce rhythm + structure + style rules in this
    # input (short_pct>0.4 -> rhythm; dialogue_ratio>0.3 -> structure;
    # description_density>1.5 -> style).
    categories = {r["category"] for r in rules}
    assert "rhythm" in categories
    assert "structure" in categories
    assert "style" in categories
    # Anti-AI rules come from ai_markers; pattern is the marker word
    # without the trailing "(N)" count suffix.
    assert len(anti_ai) == 2
    patterns = {a["pattern"] for a in anti_ai}
    assert "璜璨" in patterns
    assert "油然而生" in patterns


def test_compile_style():
    from app.models.project import StyleProfile

    profile = StyleProfile(
        name="测试风格",
        description="测试描述",
        rules_json=[
            # Rule text must be > 5 chars to survive compile_style filtering
            {"rule": "句子节奏以短句为主，偶尔叠加长描述", "weight": 0.9, "category": "rhythm"},
            {"rule": "人物对话较为口语化", "weight": 0.5, "category": "dialogue"},
        ],
        anti_ai_rules=[{"pattern": "璜璨", "replacement": "灿烂", "autoRewrite": True}],
        tone_keywords=["武侠", "江湖"],
    )
    compiled = compile_style(profile)
    # Structural section headers the prompt builder emits today
    assert "写作风格参考：测试风格" in compiled
    assert "【Anti-AI 规则】" in compiled
    assert "【风格关键词】" in compiled
    # Concrete rule content + anti-ai mapping survives
    assert "璜璨" in compiled
    assert "灿烂" in compiled
    assert "武侠" in compiled
    assert "江湖" in compiled


def test_parse_json_basic():
    result = _parse_json('{"a": 1}')
    assert result["a"] == 1


def test_parse_json_markdown():
    result = _parse_json('```json\n{"b": 2}\n```')
    assert result["b"] == 2
