from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.style_card_observations import render_style_cards


def test_conflicting_sample_styles_are_visible_and_order_independent():
    cards = [
        {'pov': 'first_person', 'sentence_rhythm': '短句'},
        {'pov': 'third_limited', 'sentence_rhythm': '长短交错，描写舒缓且充分'},
    ]
    assert render_style_cards(cards) == render_style_cards(cards[::-1])
    text = '\n'.join(render_style_cards(cards))
    for observation in ('first_person（1/2片）', 'third_limited（1/2片）', '短句（1/2片）', '长短交错，描写舒缓且充分（1/2片）'):
        assert observation in text
    assert '视角与时态服从本章既定要求' in text


def test_duplicate_tags_in_one_card_do_not_inflate_coverage():
    text = '\n'.join(render_style_cards([{'vocab_tone': ['克制', '克制', ' 克制 ']}, {}]))
    assert '克制（1/2片）' in text
    assert '克制（3/' not in text


def test_sensory_means_use_valid_per_dimension_denominators():
    text = '\n'.join(render_style_cards([
        {'sensory_mix': {'visual': 0.6, 'auditory': 0}},
        {'sensory_mix': {'visual': None, 'auditory': 0.4}},
        {'sensory_mix': {'visual': float('nan'), 'touch': 0.3}},
    ]))
    assert 'visual 60.0%（有效1片）' in text
    assert 'auditory 20.0%（有效2片）' in text
    assert 'touch 30.0%（有效1片）' in text


@pytest.mark.parametrize('invalid', [True, float('inf'), -0.1, 2, {}, 'bad'])
def test_invalid_sensory_value_does_not_erase_valid_style(invalid):
    text = '\n'.join(render_style_cards([{'sensory_mix': {'visual': invalid}, 'pacing': '先缓后急'}]))
    assert '先缓后急' in text
    assert 'visual' not in text


def test_malformed_scalar_and_string_instead_of_tag_list_are_not_injected():
    assert render_style_cards([None, [], {'pov': {'unexpected': 'third'}, 'vocab_tone': '逐字误拆', 'sensory_mix': []}]) == []


def test_truncated_variations_are_disclosed():
    text = '\n'.join(render_style_cards([{'pov': value} for value in ('a', 'b', 'c', 'd')]))
    assert '另有1种记录未列出' in text
    assert '不代表全书' in text


@pytest.mark.asyncio
async def test_card_fallback_reaches_prompt_with_stable_database_sampling():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(profile_json={
        'pov': 'third_limited', 'forbidden_tells': ['避免庄严抒情'], 'signature_moves': ['自嘲']})]
    db.execute = AsyncMock(return_value=result)
    parts = await builder._aggregate_style_cards(db, 'book')
    prompt = ContextPack(style_samples=parts).to_system_prompt(token_budget=8000)
    assert '样本中回避的表达: 避免庄严抒情' in prompt
    assert '不照搬样本禁忌或招牌' in prompt
    assert '样本常见手法: 自嘲' in prompt
    query = str(db.execute.call_args.args[0])
    assert 'style_profile_cards.created_at ASC, style_profile_cards.id ASC' in query


def test_whitespace_alias_cannot_double_count_one_sensory_dimension():
    text = '\n'.join(render_style_cards([{'sensory_mix': {'visual': 0.6, ' visual ': 0.2}}]))
    assert 'visual 60.0%（有效1片）' in text


def test_omitted_sensory_dimensions_are_disclosed():
    text = '\n'.join(render_style_cards([{'sensory_mix': {str(n): 0.1 for n in range(6)}}]))
    assert '另有1项未列出' in text
