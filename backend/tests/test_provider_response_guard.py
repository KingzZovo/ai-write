from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.services import llm_retry
from app.services.llm_retry import LLMRelayResponseError
from app.services.model_router import OpenAIProvider
from app.services.provider_response_guard import guard_chat_stream, reject_relay_error

ERROR = '⚠️ Upstream Gemini returned an empty response. secret diagnostic details'


class Stream:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            text = next(self.parts)
        except StopIteration:
            raise StopAsyncIteration
        if isinstance(text, BaseException):
            raise text
        return NS(choices=[NS(delta=NS(content=text))], usage=None)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize('text', [ERROR, ERROR[3:], '\n  ' + ERROR])
def test_rejects_observed_relay_diagnostic_without_echoing_it(text):
    with pytest.raises(LLMRelayResponseError) as exc:
        reject_relay_error(text)
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize('text', [
    '她盯着屏幕。' + ERROR, '“' + ERROR + '”他说。',
    '⚠️ 前方桥梁损坏。', 'Upstream water flooded the town.', '',
])
def test_preserves_narrative_and_similar_openings(text):
    reject_relay_error(text)


@pytest.mark.asyncio
async def test_split_diagnostic_and_initial_metadata_never_escape():
    stream = Stream([None, '', *ERROR])
    seen = []
    with pytest.raises(LLMRelayResponseError):
        async for chunk in guard_chat_stream(stream):
            seen.append(chunk)
    assert seen == []
    assert stream.closed


@pytest.mark.asyncio
async def test_normal_stream_preserves_chunks_and_closes_on_consumer_stop():
    stream = Stream(['她', '走过桥。'])
    gen = guard_chat_stream(stream)
    assert (await anext(gen)).choices[0].delta.content == '她'
    await gen.aclose()
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_retry_recovers_without_leaking_error(monkeypatch):
    monkeypatch.setattr(llm_retry, '_backoff_delay', lambda *args: 0)
    streams = [Stream([None, *ERROR]), Stream(['小说', '正文。'])]
    provider = OpenAIProvider('test')
    provider._create_chat_stream = AsyncMock(side_effect=streams)
    text = ''.join([s async for s in provider.generate_stream([], model='gemini-3.1-pro', retry_attempts=2)])
    assert text == '小说正文。'
    assert all(s.closed for s in streams)
    assert provider._create_chat_stream.await_count == 2


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_prose_and_closes():
    provider = OpenAIProvider('test')
    stream = Stream(['正文。', ConnectionError('disconnected')])
    provider._create_chat_stream = AsyncMock(return_value=stream)
    seen = []
    with pytest.raises(ConnectionError):
        async for text in provider.generate_stream([], retry_attempts=2):
            seen.append(text)
    assert seen == ['正文。']
    assert provider._create_chat_stream.await_count == 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_generate_rejects_error_in_both_response_modes(streaming):
    provider = OpenAIProvider('test')
    stream = Stream([ERROR])
    provider._create_chat_stream = AsyncMock(return_value=stream)
    provider._client = NS(chat=NS(completions=NS(create=AsyncMock(
        return_value=NS(choices=[NS(message=NS(content=ERROR))], usage=None)))))
    with pytest.raises(LLMRelayResponseError):
        await provider.generate([], model='gemini-3.1-pro', stream=streaming, retry_attempts=1)
    if streaming:
        assert stream.closed


@pytest.mark.asyncio
async def test_consumer_close_releases_provider_stream():
    provider = OpenAIProvider('test')
    stream = Stream(['正文', '后续'])
    provider._create_chat_stream = AsyncMock(return_value=stream)
    result = provider.generate_stream([], retry_attempts=1)
    assert await anext(result) == '正文'
    await result.aclose()
    assert stream.closed


@pytest.mark.asyncio
async def test_cancel_while_prefix_pending_closes_without_retry():
    stream = Stream([])
    entered = asyncio.Event()
    async def waiting():
        try:
            yield NS(choices=[NS(delta=NS(content='⚠️ '))], usage=None)
            entered.set()
            await asyncio.Event().wait()
        finally:
            await stream.close()
    provider = OpenAIProvider('test')
    provider._create_chat_stream = AsyncMock(return_value=waiting())
    async def consume():
        return [s async for s in provider.generate_stream([], retry_attempts=2)]
    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    assert provider._create_chat_stream.await_count == 1
