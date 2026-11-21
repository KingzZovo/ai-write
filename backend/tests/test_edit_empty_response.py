from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import text_rewriter
from app.services.agents import style_agent
from app.services.provider_response_guard import require_nonempty_stream


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['rewrite', 'polish', 'profile'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_all_edit_entries_reject_whitespace_reply(monkeypatch, mode, streaming):
    async def stream(**kwargs):
        yield ' \n'
        yield '\t'
    router = NS(generate=AsyncMock(return_value=NS(text=' \n', finish_reason='stop')), generate_stream=stream)
    module = text_rewriter if mode == 'rewrite' else style_agent
    monkeypatch.setattr(module, 'get_model_router', lambda: router)
    service = module.TextRewriter() if mode == 'rewrite' else module.StyleAgent()
    method = {'rewrite':'rewrite', 'polish':'polish', 'profile':'polish_with_profile'}[mode]
    args = ('原稿。','restructure') if mode == 'rewrite' else ('原稿。',{}) if mode == 'profile' else ('原稿。',)
    with pytest.raises(ValueError, match='provider_empty_response'):
        if streaming:
            async for _ in getattr(service, method+'_stream')(*args): pass
        else:
            await getattr(service, method)(*args)


@pytest.mark.asyncio
async def test_stream_preserves_whitespace_and_closes_source_on_failure():
    closed = []
    async def stream():
        try:
            yield '  '
            yield '完整正文。\n'
        finally:
            closed.append(True)
    assert [s async for s in require_nonempty_stream(stream())] == ['  ', '完整正文。\n']
    assert closed == [True]


@pytest.mark.asyncio
async def test_existing_failure_is_not_reclassified_as_empty():
    async def stream():
        yield ' '
        raise RuntimeError('transport failure')
    with pytest.raises(RuntimeError, match='transport failure'):
        async for _ in require_nonempty_stream(stream()): pass


@pytest.mark.asyncio
async def test_rewrite_sse_empty_stream_emits_error_without_completed(monkeypatch):
    import json
    from app.api.rewrite import RewriteRequest, rewrite_text
    async def stream(**kwargs):
        yield '  '
    monkeypatch.setattr(text_rewriter, 'get_model_router', lambda: NS(generate_stream=stream))
    response = await rewrite_text(RewriteRequest(selected_text='原稿。', operation='restructure'))
    events = []
    async for raw in response.body_iterator:
        value = raw.removeprefix('data: ').strip()
        if value != '[DONE]': events.append(json.loads(value))
    assert any(event.get('code') == 'rewrite_empty_response' for event in events)
    assert not any(event.get('status') == 'completed' for event in events)
