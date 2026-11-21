"""Reference statistics must not become generic Chinese fiction quotas."""
from types import SimpleNamespace

import pytest

from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.style_reference_observations import render_observations


@pytest.mark.parametrize('data', [None, [], {}, {'sentence': []}, {'dialogue': {'ratio': None}}])
def test_empty_or_unmeasured_reference_does_not_invent_zero_targets(data):
    assert render_observations(data) == ''


@pytest.mark.parametrize('invalid', [-1, float('nan'), float('inf'), True, {}, 'invalid', 1.5])
def test_invalid_ratio_does_not_suppress_other_valid_measurements(invalid):
    text = render_observations({'dialogue': {'ratio': invalid}, 'sentence': {'mean_chars': 24}})
    assert '对话文字占比：' not in text
    assert '平均句长：24字' in text


def test_measured_zero_is_retained_without_creating_missing_metrics():
    text = render_observations({'dialogue': {'ratio': 0}})
    assert '对话文字占比：0%' in text
    assert '比喻统计：' not in text
    assert '口语助词命中：' not in text
    assert '不是本章目标、上下限或合格线' in text


def test_reference_statistics_reach_actual_prompt_with_all_profile_sections():
    profile = SimpleNamespace(
        name='市井亲情', source_book='参考甲',
        rules_json=['对话符合人物年龄和身份'],
        anti_ai_rules=['不使用作者明确排除的套语'],
        tone_keywords=['克制'],
        sample_passages=['不应复制的原作段落'],
        config_json={'dosage_profile': {
            'source': '参考乙', 'dialogue': {'ratio': 0.32},
            'metaphor': {'total_per_kchar': 1.8},
            'psychology': {'neutral_words_per_kchar': 3.2},
            'sentence': {'mean_chars': 22}, 'paragraph': {'mean_chars': 46},
            'colloquial': {'particles_per_kchar': 0.5},
        }},
    )
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    parts = builder._render_style_profile(profile)
    prompt = ContextPack(style_samples=parts).to_system_prompt(token_budget=8000)
    for expected in ('对话符合人物年龄和身份', '不使用作者明确排除的套语', '克制',
                     '参考乙', '对话文字占比：32%', '平均段长：46字', '口语助词命中：0.5次/千字'):
        assert expected in prompt
    for invented in ('7000', '27 字', '江南特色', '能用口语骂人', '每章不得超过 2 次', '不应复制的原作段落'):
        assert invented not in prompt
    assert '不为接近数字增删文字' in prompt


def test_non_numeric_section_and_legacy_chapter_estimate_cannot_create_counts():
    text = render_observations({'source': '参考书', 'psychology': {'pattern_per_chapter_7k': 9},
                               'sentence': 'malformed', 'paragraph': {'mean_chars': '42'}})
    assert '平均段长：42字' in text
    assert '心理套语命中：' not in text
    assert '平均句长：' not in text
