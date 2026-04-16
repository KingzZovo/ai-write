"""Unit tests for core services (no LLM/DB required)."""

import pytest
from app.services.style_detection import detect_style_features, features_to_rules
from app.services.style_compiler import compile_style
from app.services.quality_scorer import _parse_json


def test_style_detection_basic():
    text = "他走在雨中的街道上。路灯昏黄的光映在湿漉漉的柏油路面。" * 20
    features = detect_style_features(text)
    assert "basic" in features
    assert features["basic"]["total_chars"] > 0
    assert "dialogue_ratio" in features
    assert "ai_markers" in features
    assert "top_words" in features


def test_style_detection_filters_names():
    text = "张三走在路上。李四在旁边说话。王五跑过来了。他们三人一起前行。" * 30
    features = detect_style_features(text)
    top_word_texts = [w["word"] for w in features["top_words"]]
    # Person names should be filtered out
    assert "张三" not in top_word_texts
    assert "李四" not in top_word_texts
    assert "王五" not in top_word_texts


def test_features_to_rules():
    features = {
        "sentence_distribution": {"short_pct": 0.5, "medium_pct": 0.3, "long_pct": 0.2},
        "dialogue_ratio": 0.4,
        "description_density": 2.0,
        "ai_markers": ["璀璨(3)", "油然而生(1)"],
        "top_words": [{"word": "剑", "count": 10}],
    }
    rules, anti_ai = features_to_rules(features)
    assert len(rules) > 0
    assert len(anti_ai) == 2
    assert anti_ai[0]["pattern"] == "璀璨"


def test_compile_style():
    from app.models.project import StyleProfile
    profile = StyleProfile(
        name="测试风格",
        description="测试描述",
        rules_json=[
            {"rule": "高权重规则", "weight": 0.9, "category": "test"},
            {"rule": "低权重规则", "weight": 0.5, "category": "test"},
        ],
        anti_ai_rules=[{"pattern": "璀璨", "replacement": "灿烂", "autoRewrite": True}],
        tone_keywords=["武侠", "江湖"],
    )
    compiled = compile_style(profile)
    assert "必须保持" in compiled
    assert "参考风格" in compiled
    assert "璀璨" in compiled
    assert "武侠" in compiled


def test_parse_json_basic():
    result = _parse_json('{"a": 1}')
    assert result["a"] == 1


def test_parse_json_markdown():
    result = _parse_json('```json\n{"b": 2}\n```')
    assert result["b"] == 2
