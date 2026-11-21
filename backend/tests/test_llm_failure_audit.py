"""Provider failures must survive owned telemetry-session rollback/close."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_pipeline_control import runtime_db
from app.models import Base
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.models.call_log import LLMCallLog
from app.services.model_router import ModelRouter, TaskRouteConfig, GenerationResult, TokenUsage

MODES = ['generate', 'generate_stream', 'generate_by_route', 'stream_by_route',
         'generate_with_tier_fallback', 'stream_with_tier_fallback']


async def setup(runtime_db, monkeypatch, outcome):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[
            LLMEndpoint.__table__, PromptAsset.__table__, LLMCallLog.__table__]))
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    class Provider:
        async def generate(self, **kwargs):
            if outcome == 'cancel':
                raise asyncio.CancelledError('caller stopped')
            raise ValueError('provider unavailable')
        async def generate_stream(self, **kwargs):
            if outcome in {'partial', 'close'}:
                yield '已经返回的正文'
            await self.generate(**kwargs)
    router = ModelRouter()
    router.register_provider('first', Provider())
    router.task_routing['audit-test'] = TaskRouteConfig('first', 'model-first')
    router._endpoint_defaults = {'first': 'model-first'}
    router._endpoint_tiers = {'first': 'flagship'}
    route = SimpleNamespace(endpoint_id='first', model='model-first', temperature=0.2, max_tokens=32)
    return router, route


def invoke(router, route, mode, r):
    kw = dict(messages=[{'role': 'user', 'content': '原创测试'}],
              _log_meta={'chapter_id': r.chapter, 'project_id': r.project})
    if mode.endswith('_by_route'):
        return getattr(router, mode)(route, **kw)
    return getattr(router, mode)('audit-test', **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('outcome', ['error', 'cancel'])
async def test_failure_and_cancellation_are_committed(runtime_db, monkeypatch, mode, outcome):
    router, route = await setup(runtime_db, monkeypatch, outcome)
    expected = asyncio.CancelledError if outcome == 'cancel' else (ValueError, RuntimeError)
    with pytest.raises(expected):
        call = invoke(router, route, mode, runtime_db)
        if 'stream' in mode:
            async for _ in call:
                pass
        else:
            await call
    async with runtime_db.sessions() as db:
        row = (await db.scalars(select(LLMCallLog))).one()
        assert row.status == 'error'
        assert ('CancelledError' if outcome == 'cancel' else 'ValueError') in row.error_message
        assert row.model == 'model-first'
        assert row.chapter_id == runtime_db.chapter


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [m for m in MODES if 'stream' in m])
@pytest.mark.parametrize('outcome', ['partial', 'close'])
async def test_partial_stream_and_explicit_close_are_not_logged_as_success(runtime_db, monkeypatch, mode, outcome):
    router, route = await setup(runtime_db, monkeypatch, outcome)
    call = invoke(router, route, mode, runtime_db)
    assert await anext(call) == '已经返回的正文'
    if outcome == 'close':
        await call.aclose()
    else:
        with pytest.raises(ValueError):
            await anext(call)
    async with runtime_db.sessions() as db:
        row = (await db.scalars(select(LLMCallLog))).one()
        assert row.status == 'error'
        assert row.response_text == '已经返回的正文'
        assert ('GeneratorExit' if outcome == 'close' else 'ValueError') in row.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['generate_with_tier_fallback', 'stream_with_tier_fallback'])
async def test_failed_attempt_is_visible_before_successful_fallback(runtime_db, monkeypatch, mode):
    router, route = await setup(runtime_db, monkeypatch, 'error')
    class Success:
        async def generate(self, **kwargs):
            async with runtime_db.sessions() as db:
                row = (await db.scalars(select(LLMCallLog))).one()
                assert row.status == 'error' and row.attempt_index == 0
            return GenerationResult(text='完成', model='model-second', provider='test', usage=TokenUsage())
        async def generate_stream(self, **kwargs):
            yield (await self.generate(**kwargs)).text
    router.register_provider('second', Success())
    router._endpoint_defaults['second'] = 'model-second'
    router._endpoint_tiers['second'] = 'standard'
    call = invoke(router, route, mode, runtime_db)
    if 'stream' in mode:
        assert ''.join([x async for x in call]) == '完成'
    else:
        assert (await call).text == '完成'
    async with runtime_db.sessions() as db:
        rows = (await db.scalars(select(LLMCallLog).order_by(LLMCallLog.attempt_index))).all()
        assert [r.status for r in rows] == ['error', 'ok']
        assert [r.attempt_index for r in rows] == [0, 1]
        assert 'ValueError' in rows[1].fallback_reason


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_fails', [False, True])
async def test_log_commit_failure_preserves_provider_result_or_error(runtime_db, monkeypatch, provider_fails):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    router, route = await setup(runtime_db, monkeypatch, 'error')
    @asynccontextmanager
    async def broken_commit_session():
        async with runtime_db.sessions() as db:
            db.commit = AsyncMock(side_effect=RuntimeError('telemetry commit unavailable'))
            yield db
    monkeypatch.setattr('app.db.session.async_session_factory', broken_commit_session)
    if not provider_fails:
        router.providers['first'].generate = AsyncMock(return_value=GenerationResult(
            text='正文保留', model='model-first', provider='test', usage=TokenUsage()))
    call = invoke(router, route, 'generate', runtime_db)
    if provider_fails:
        with pytest.raises(ValueError, match='provider unavailable'):
            await call
    else:
        assert (await call).text == '正文保留'
    async with runtime_db.sessions() as db:
        assert not (await db.scalars(select(LLMCallLog))).all()
