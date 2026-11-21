"""Shared cache freshness and complete/failed delivery counters."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from app.services import prompt_cache, prompt_loader, prompt_registry
from tests.test_c3_prompt_cache import _make_asset, _fake_db_returning


@pytest.fixture(autouse=True)
def reset_cache():
    prompt_cache.reset_for_tests()
    yield
    prompt_cache.reset_for_tests()


@pytest.mark.asyncio
async def test_legacy_loader_observes_admin_invalidation(monkeypatch):
    old=_make_asset(task_type='outline_book'); old.system_prompt='旧版'
    new=_make_asset(task_type='outline_book'); new.system_prompt='新版'
    current=[old]
    @asynccontextmanager
    async def factory():
        yield _fake_db_returning(current[0])
    monkeypatch.setattr('app.db.session.async_session_factory', factory)
    assert await prompt_loader.load_prompt('outline_book') == '旧版'
    current[0]=new
    prompt_cache.invalidate('outline_book')
    assert await prompt_loader.load_prompt('outline_book') == '新版'


@pytest.mark.asyncio
async def test_fallback_is_not_cached_across_callers(monkeypatch):
    @asynccontextmanager
    async def factory():
        yield _fake_db_returning(None)
    monkeypatch.setattr('app.db.session.async_session_factory', factory)
    assert await prompt_loader.load_prompt('missing', '阶段甲') == '阶段甲'
    assert await prompt_loader.load_prompt('missing', '阶段乙') == '阶段乙'


def setup_router(monkeypatch, router):
    route=SimpleNamespace(prompt_id=uuid4(), system_prompt='注册提示词')
    monkeypatch.setattr(prompt_registry, '_resolve_route_and_tier_cached', AsyncMock(return_value=(route,'standard')))
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', AsyncMock(return_value=router))
    return str(route.prompt_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['有效正文', '   '])
async def test_text_delivery_counts_non_whitespace_output(monkeypatch,text):
    router=SimpleNamespace(generate_with_tier_fallback=AsyncMock(return_value=SimpleNamespace(text=text)))
    aid=setup_router(monkeypatch,router)
    await prompt_registry.run_text_prompt('outline_book','任务',None)
    category='success' if text.strip() else 'fail'
    assert prompt_cache.pending_counts()[category] == {aid:1}


@pytest.mark.asyncio
async def test_text_exception_is_counted_and_preserved(monkeypatch):
    router=SimpleNamespace(generate_with_tier_fallback=AsyncMock(side_effect=TimeoutError('模型超时')))
    aid=setup_router(monkeypatch,router)
    with pytest.raises(TimeoutError):
        await prompt_registry.run_text_prompt('outline_book','任务',None)
    assert prompt_cache.pending_counts()['fail'] == {aid:1}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['complete','empty','exception','close'])
async def test_stream_counts_terminal_delivery_and_closes_provider(monkeypatch,mode):
    closed=[]
    async def stream(*args,**kwargs):
        try:
            yield ' ' if mode == 'empty' else '有效正文'
            if mode == 'exception':
                raise TimeoutError('模型中断')
            yield ''
        finally:
            closed.append(True)
    aid=setup_router(monkeypatch,SimpleNamespace(stream_with_tier_fallback=stream))
    output=prompt_registry.stream_text_prompt('outline_book','任务',None)
    if mode == 'close':
        await anext(output)
        await output.aclose()
    elif mode == 'exception':
        with pytest.raises(TimeoutError):
            async for _ in output: pass
    else:
        async for _ in output: pass
    assert closed == [True]
    counts=prompt_cache.pending_counts()
    category='success' if mode == 'complete' else 'fail'
    assert counts[category] == {aid:1}
    assert counts['fail' if category == 'success' else 'success'] == {}


@pytest.mark.asyncio
async def test_stream_task_cancellation_closes_provider_and_counts_once(monkeypatch):
    import asyncio
    started=asyncio.Event()
    closed=[]
    async def stream(*args,**kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield 'unreachable'
        finally:
            closed.append(True)
    aid=setup_router(monkeypatch,SimpleNamespace(stream_with_tier_fallback=stream))
    async def consume():
        async for _ in prompt_registry.stream_text_prompt('outline_book','任务',None):
            pass
    task=asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert prompt_cache.pending_counts()['fail'] == {aid:1}
