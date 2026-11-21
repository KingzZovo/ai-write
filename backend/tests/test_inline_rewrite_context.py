from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json
import pytest
from app.services import text_rewriter as module


@pytest.fixture
def rewriter(monkeypatch):
    router = NS(generate=AsyncMock(return_value=NS(text='改稿。', finish_reason='stop')))
    monkeypatch.setattr(module, 'get_model_router', lambda: router)
    return module.TextRewriter()


@pytest.mark.asyncio
async def test_complete_context_and_custom_request_reach_model(rewriter):
    before, after = '远处事实。' + '前文。'*400, '后文。'*400 + '最终结果。'
    await rewriter.rewrite('所选正文。', 'custom', '不要改变人物态度', before, after)
    prompt = rewriter.router.generate.call_args.kwargs['messages'][1]['content']
    assert before in prompt and after in prompt and '不要改变人物态度' in prompt
    assert '只读' in prompt and '只输出所选原文的替换文本' in prompt


@pytest.mark.asyncio
async def test_oversized_context_is_not_silently_dropped(rewriter):
    with pytest.raises(ValueError, match='提示预算'):
        await rewriter.rewrite('原稿。', 'restructure', context_before='前'*module.REWRITE_MAX_INPUT_CHARS)
    rewriter.router.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'content_filter', 'tool_calls'])
async def test_incomplete_nonstream_rewrite_not_returned(rewriter, reason):
    rewriter.router.generate.return_value = NS(text='已带句号的半稿。', finish_reason=reason)
    with pytest.raises(ValueError, match='provider_incomplete_response'):
        await rewriter.rewrite('完整原稿。', 'restructure')


@pytest.mark.asyncio
async def test_sse_partial_error_never_emits_completed(monkeypatch):
    from app.api.rewrite import RewriteRequest, rewrite_text
    class Rewriter:
        async def rewrite_stream(self, **kwargs):
            yield '半篇改写。'
            raise ValueError('provider_incomplete_response')
    monkeypatch.setattr(module, 'TextRewriter', Rewriter)
    response = await rewrite_text(RewriteRequest(selected_text='原稿。', operation='restructure'))
    events = []
    async for item in response.body_iterator:
        value = item.removeprefix('data: ').strip()
        if value != '[DONE]': events.append(json.loads(value))
    assert any(e.get('text') == '半篇改写。' for e in events)
    assert any(e.get('code') == 'rewrite_incomplete_response' for e in events)
    assert not any(e.get('status') == 'completed' for e in events)
