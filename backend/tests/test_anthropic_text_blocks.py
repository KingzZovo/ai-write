from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.model_router import AnthropicProvider


def provider_for(blocks, stop_reason='end_turn'):
    provider = AnthropicProvider('test')
    provider._client = NS(messages=NS(create=AsyncMock(return_value=NS(
        content=blocks, usage=NS(input_tokens=3, output_tokens=4), stop_reason=stop_reason))))
    return provider


@pytest.mark.asyncio
async def test_all_text_blocks_join_in_order_without_inventing_separators():
    provider = provider_for([
        NS(type='thinking', thinking='不要进入小说的分析'),
        NS(type='text', text='她推开'),
        NS(type='redacted_thinking', data='隐藏内容'),
        NS(type='text', text='门。\n\n'),
        NS(type='tool_use', text='即使有text属性也不是正文', input={'secret': 'value'}),
        NS(type='text', text='屋里没有人。'),
    ])
    result = await provider.generate([], model='test')
    assert result.text == '她推开门。\n\n屋里没有人。'
    assert result.finish_reason == 'end_turn'
    assert result.usage.input_tokens == 3 and result.usage.output_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('blocks', [[], [NS(type='thinking', thinking='分析')], [NS(text='没有类型的对象')]])
async def test_nontext_or_untyped_content_is_never_treated_as_prose(blocks):
    result = await provider_for(blocks).generate([], model='test')
    assert result.text == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, 123, {'text': '不应强制字符串化'}])
async def test_malformed_text_block_does_not_return_partial_prose(invalid):
    provider = provider_for([NS(type='text', text='前半篇。'), NS(type='text', text=invalid)])
    with pytest.raises(ValueError, match='^invalid_provider_text_block$'):
        await provider.generate([], model='test')


@pytest.mark.asyncio
async def test_empty_text_block_preserves_following_prose_and_truncation_metadata():
    result = await provider_for([NS(type='text', text=''), NS(type='text', text='正文未完')], 'max_tokens').generate([], model='test')
    assert result.text == '正文未完' and result.finish_reason == 'max_tokens'
