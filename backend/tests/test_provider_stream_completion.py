from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.services.model_router import AnthropicProvider, OpenAIProvider


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'content_filter', 'tool_calls', 'unknown', 'stop', None])
async def test_openai_stream_checks_final_reason_after_usage_and_closes(reason):
    class Stream:
        closed = False
        def __aiter__(self): return self.parts()
        async def parts(self):
            yield NS(choices=[NS(delta=NS(content='正文已经带句号。'), finish_reason=None)], usage=None)
            yield NS(choices=[NS(delta=NS(content=None), finish_reason=reason)], usage=None)
            yield NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=2, total_tokens=12))
        async def close(self): self.closed = True
    raw = Stream()
    provider = OpenAIProvider('test')
    provider._create_chat_stream = AsyncMock(return_value=raw)
    chunks, usage = [], {}
    async def consume():
        async for chunk in provider.generate_stream([], model='test-model', retry_attempts=1, usage_box=usage):
            chunks.append(chunk)
    if reason in ('stop', None):
        await consume()
    else:
        with pytest.raises(ValueError, match='^provider_incomplete_response$'):
            await consume()
    assert chunks == ['正文已经带句号。'] and raw.closed
    assert usage == {'input_tokens': 10, 'output_tokens': 2}
    provider._create_chat_stream.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['max_tokens', 'tool_use', 'end_turn', 'stop_sequence', None, 'final_error'])
async def test_anthropic_stream_completion_is_not_swallowed_as_metrics_error(reason):
    class Stream:
        closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True
        @property
        def text_stream(self): return self.parts()
        async def parts(self): yield '正文已经带句号。'
        async def get_final_message(self):
            if reason == 'final_error': raise RuntimeError('final unavailable')
            return NS(stop_reason=reason, usage=None)
    raw = Stream()
    provider = AnthropicProvider('test')
    provider._client = NS(messages=NS(stream=lambda **kwargs: raw))
    chunks = []
    async def consume():
        async for chunk in provider.generate_stream([], model='test-model'): chunks.append(chunk)
    if reason == 'final_error':
        with pytest.raises(RuntimeError, match='final unavailable'): await consume()
    elif reason in ('max_tokens', 'tool_use'):
        with pytest.raises(ValueError, match='^provider_incomplete_response$'): await consume()
    else:
        await consume()
    assert chunks == ['正文已经带句号。'] and raw.closed
