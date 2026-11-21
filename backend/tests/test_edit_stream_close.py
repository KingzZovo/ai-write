from types import SimpleNamespace as NS
import pytest
from app.services import text_rewriter, model_router
from app.services.agents import style_agent


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['rewrite', 'polish', 'profile', 'sse'])
async def test_closing_edit_after_first_text_immediately_closes_provider(monkeypatch, mode):
    closed = []
    class Provider:
        async def generate_stream(self, **kwargs):
            try:
                yield '首段。'
                yield '不应再读取的第二段。'
            finally:
                closed.append(True)
    router = model_router.ModelRouter()
    monkeypatch.setattr(router, '_get_route', lambda _: NS(provider_key='fake', temperature=.5, max_tokens=100))
    monkeypatch.setattr(router, '_get_provider', lambda _: Provider())
    monkeypatch.setattr(router, '_resolve_model', lambda _: 'fake-model')
    monkeypatch.setattr(text_rewriter, 'get_model_router', lambda: router)
    monkeypatch.setattr(style_agent, 'get_model_router', lambda: router)
    if mode == 'rewrite': stream = text_rewriter.TextRewriter().rewrite_stream('原稿。','restructure')
    elif mode == 'polish': stream = style_agent.StyleAgent().polish_stream('原稿。')
    elif mode == 'profile': stream = style_agent.StyleAgent().polish_with_profile_stream('原稿。',{})
    else:
        from app.api.rewrite import RewriteRequest, rewrite_text
        response = await rewrite_text(RewriteRequest(selected_text='原稿。',operation='restructure'))
        stream = response.body_iterator
        assert 'rewriting' in await anext(stream)
    item = await anext(stream)
    if mode == 'sse':
        import json
        item = json.loads(item.removeprefix('data: '))['text']
    assert item == '首段。'
    assert not closed
    await stream.aclose()
    assert closed == [True]  # no garbage collection or event-loop sleep needed


@pytest.mark.asyncio
async def test_logged_router_closes_provider_before_log_commit(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from app.db import session
    from app.services import llm_call_logger
    events = []
    class Provider:
        async def generate_stream(self, **kwargs):
            try:
                yield '首段。'
                yield '下一段。'
            finally:
                events.append('provider_closed')
    @asynccontextmanager
    async def sessions(): yield NS()
    @asynccontextmanager
    async def log(**kwargs):
        try: yield NS(add_chunk=lambda text: None, set_usage=lambda *args: None)
        finally: events.append('log_closed')
    async def commit(*args, **kwargs): events.append('committed')
    monkeypatch.setattr(session, 'async_session_factory', sessions)
    monkeypatch.setattr(llm_call_logger, 'log_llm_call', log)
    monkeypatch.setattr(model_router, '_commit_llm_call_log', commit)
    router = model_router.ModelRouter()
    monkeypatch.setattr(router, '_get_route', lambda _: NS(provider_key='fake', temperature=.5, max_tokens=100))
    monkeypatch.setattr(router, '_get_provider', lambda _: Provider())
    monkeypatch.setattr(router, '_resolve_model', lambda _: 'fake')
    stream = router.generate_stream('polishing', [], _log_meta={})
    assert await anext(stream) == '首段。'
    await stream.aclose()
    assert events == ['provider_closed', 'log_closed', 'committed']
