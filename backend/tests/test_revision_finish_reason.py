from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.model_router import AnthropicProvider, GenerationResult, OpenAIProvider, TokenUsage
from app.services.auto_revise import revise_spans


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'length', 'content_filter', None])
async def test_openai_nonstream_preserves_finish_reason(reason):
    provider = OpenAIProvider('test')
    provider._client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=NS(
        choices=[NS(message=NS(content='正文。'), finish_reason=reason)], usage=None)))))
    result = await provider.generate([], model='test-model', stream=False, retry_attempts=1)
    assert result.text == '正文。' and result.finish_reason == reason


@pytest.mark.asyncio
async def test_internal_stream_keeps_terminal_reason_after_usage_only_chunk():
    class Stream:
        closed = False
        def __aiter__(self): return self.parts()
        async def parts(self):
            yield NS(choices=[NS(delta=NS(content='正文。'), finish_reason=None)], usage=None)
            yield NS(choices=[NS(delta=NS(content=None), finish_reason='length')], usage=None)
            yield NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=2, total_tokens=12))
        async def close(self): self.closed = True
    stream = Stream()
    provider = OpenAIProvider('test')
    provider._create_chat_stream = AsyncMock(return_value=stream)
    result = await provider.generate([], model='test-model', stream=True, retry_attempts=1)
    assert result.text == '正文。' and result.finish_reason == 'length'
    assert result.usage.total_tokens == 12 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['end_turn', 'max_tokens', 'tool_use'])
async def test_anthropic_preserves_stop_reason(reason):
    provider = AnthropicProvider('test')
    provider._client = NS(messages=NS(create=AsyncMock(return_value=NS(
        content=[NS(type='text', text='正文。')], usage=NS(input_tokens=1, output_tokens=1), stop_reason=reason))))
    result = await provider.generate([], model='test-model')
    assert result.finish_reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter', 'tool_calls', 'tool_use', 'unknown'])
async def test_known_incomplete_response_cannot_be_spliced_even_when_it_ends_with_punctuation(monkeypatch, reason):
    from app.services import prompt_registry
    call = AsyncMock(return_value=GenerationResult('看似完整但已经截断的替换正文。', TokenUsage(), 'test', 'test', reason))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', call)
    text = '这一段是原来的完整正文。'
    with pytest.raises(ValueError, match='^targeted_revision_incomplete_response$'):
        await revise_spans(text, [{'quote': '原来的完整正文'}], project_id='p', chapter_id='c')
    call.assert_awaited_once()
    assert call.call_args.kwargs['project_id'] == 'p'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'end_turn', 'stop_sequence', None])
async def test_normal_or_unspecified_legacy_finish_reason_keeps_existing_route(monkeypatch, reason):
    from app.services import prompt_registry
    revised = '这一段是修订后的完整正文。'
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', AsyncMock(return_value=GenerationResult(
        revised, TokenUsage(), 'test', 'test', reason)))
    result = await revise_spans('这一段是原来的完整正文。', [{'quote': '原来的完整正文'}])
    assert result.text == revised and result.spans_revised == 1
