from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services.agents import style_agent as module


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(module, 'get_model_router', lambda: NS(generate=AsyncMock(return_value=NS(text='原稿。', finish_reason='stop'))))
    return module.StyleAgent()


def test_profile_missing_invalid_numbers_are_not_zero_or_crashes():
    text = module._build_style_instruction_from_profile({'sentence_ratio': {'short': .2, 'long': float('nan')}, 'dialogue_ratio': True, 'rhetoric_profile': {'比喻': 'bad'}, 'pov_type': 'omniscient'})
    assert '短句20.0%' in text and '中句' not in text and '长句' not in text
    assert '档案对白观察' not in text and '档案修辞观察' not in text
    assert '不据此转换' in text and '不要求本段凑比例' in text


def test_references_are_complete_data_not_fake_assistant_examples(agent):
    sample = '参考开头。' + '参考。' * 600 + '参考结尾。'
    messages = agent._build_profile_messages('本稿。', {}, [sample, '片段二', '片段三', '片段四'])
    assert all(m['role'] != 'assistant' for m in messages)
    assert sample in messages[1]['content'] and '片段四' in messages[1]['content']
    assert messages[-1]['content'].endswith('本稿。')


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', [False, True])
async def test_incomplete_polish_not_returned(agent, profile):
    agent.router.generate.return_value = NS(text='半稿。', finish_reason='length')
    with pytest.raises(ValueError, match='provider_incomplete_response'):
        if profile: await agent.polish_with_profile('原稿。', {})
        else: await agent.polish('原稿。', '保持原有声音')


@pytest.mark.asyncio
async def test_oversized_reference_stops_before_model(agent):
    with pytest.raises(ValueError, match='提示预算'):
        await agent.polish_with_profile('原稿。', {}, ['参'*24000])
    agent.router.generate.assert_not_awaited()


def test_manual_author_instruction_is_preserved(agent):
    messages = agent._build_messages('原稿。', '对白偏少；不要改变第一人称。')
    assert '对白偏少；不要改变第一人称。' in messages[0]['content']
