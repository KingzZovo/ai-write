from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services.polish_candidate import create_polish_candidate, POLISH_INPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_complete_source_is_one_call_without_splitting_or_postprocessing():
    text = '她说：“不必了。”\n' * 300
    candidate = text + '\n他没有回答。\n'
    call = AsyncMock(return_value=NS(text=candidate, finish_reason='stop'))
    result = await create_polish_candidate(text, instruction='保持克制', llm_call=call)
    call.assert_awaited_once()
    prompt = call.call_args.kwargs['messages'][1]['content']
    assert text in prompt and '保持克制' in prompt
    assert result.text == candidate and result.status == 'needs_review'
    assert len(result.source_hash) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', '文' * POLISH_INPUT_MAX_CHARS])
async def test_unusable_or_oversized_source_is_explicitly_skipped_without_truncation(text):
    call = AsyncMock()
    result = await create_polish_candidate(text, llm_call=call)
    assert result.status == 'skipped' and result.text == '' and result.reason
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter'])
async def test_truncated_polish_is_not_exposed_as_candidate(reason):
    with pytest.raises(ValueError, match='provider_incomplete_response'):
        await create_polish_candidate('完整原稿。', llm_call=AsyncMock(return_value=NS(text='半稿。', finish_reason=reason)))


@pytest.mark.asyncio
async def test_unchanged_is_not_presented_as_a_new_polish():
    result = await create_polish_candidate('原稿。', llm_call=AsyncMock(return_value=NS(text='原稿。', finish_reason='stop')))
    assert result.status == 'unchanged' and result.text == ''
