import json
import pytest
from app.services.rewrite_errors import RewriteContextLimitError, rewrite_error_payload


@pytest.mark.parametrize('error,code', [
    (RewriteContextLimitError('private details'), 'rewrite_context_too_large'),
    (ValueError('provider_incomplete_response'), 'rewrite_incomplete_response'),
    (ValueError('provider_empty_response'), 'rewrite_empty_response'),
    (TimeoutError('private details'), 'rewrite_timeout'),
    (RuntimeError('private endpoint and key'), 'rewrite_failed'),
])
def test_public_error_is_bounded_and_does_not_echo_provider_details(error, code):
    result = rewrite_error_payload(error)
    assert result['code'] == code and '原文未被修改' in result['error']
    assert 'private' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('initialization_failure', [False, True])
async def test_sse_error_does_not_leak_details_even_during_initialization(monkeypatch, initialization_failure):
    from app.api.rewrite import RewriteRequest, rewrite_text
    from app.services import text_rewriter
    class Rewriter:
        def __init__(self):
            if initialization_failure: raise RuntimeError('private endpoint and key')
        async def rewrite_stream(self, **kwargs):
            yield '未完稿。'
            raise RuntimeError('private endpoint and key')
    monkeypatch.setattr(text_rewriter, 'TextRewriter', Rewriter)
    response = await rewrite_text(RewriteRequest(selected_text='原稿。', operation='restructure'))
    events = []
    async for raw in response.body_iterator:
        assert 'private' not in raw
        value = raw.removeprefix('data: ').strip()
        if value != '[DONE]': events.append(json.loads(value))
    assert events[-1]['code'] == 'rewrite_failed'
    assert not any(e.get('status') == 'completed' for e in events)
