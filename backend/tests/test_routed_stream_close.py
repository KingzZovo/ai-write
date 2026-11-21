from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
import asyncio
import pytest
from app.services import model_router


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['route', 'tier'])
@pytest.mark.parametrize('logged', [False, True])
async def test_explicit_and_tier_stream_close_without_fallback_and_finalize_log(monkeypatch, mode, logged):
    from app.db import session
    from app.services import llm_call_logger
    events = []
    class Provider:
        def __init__(self, name): self.name = name
        async def generate_stream(self, **kwargs):
            events.append(self.name + '_opened')
            try:
                yield '原始正文。'
                yield '后续正文。'
            finally:
                events.append(self.name + '_closed')
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
    router.register_provider('first', Provider('first'))
    router.register_provider('second', Provider('second'))
    monkeypatch.setattr(router, '_build_tier_attempts', lambda **kw: [('standard','first','model'),('fallback','second','model')])
    route = NS(endpoint_id='first',model='model',temperature=.5,max_tokens=100)
    meta = {} if logged else None
    stream = router.stream_by_route(route, [], _log_meta=meta) if mode == 'route' else router.stream_with_tier_fallback('generation', [], route=route, _log_meta=meta)
    assert await anext(stream) == '原始正文。'
    await stream.aclose()
    assert events == ['first_opened','first_closed'] + (['log_closed','committed'] if logged else [])


@pytest.mark.asyncio
async def test_cancel_waiting_tier_stream_does_not_start_fallback(monkeypatch):
    entered = asyncio.Event()
    closed = []
    class Provider:
        async def generate_stream(self, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield 'never'
            finally: closed.append(True)
    router = model_router.ModelRouter()
    router.register_provider('first', Provider())
    monkeypatch.setattr(router, '_build_tier_attempts', lambda **kw: [('standard','first','model'),('fallback','missing','model')])
    route = NS(temperature=.5,max_tokens=100)
    stream = router.stream_with_tier_fallback('generation', [], route=route)
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed == [True]
