"""v1.7 X2 unit tests: verify knowledge_tasks._run_async and
style_tasks._run_async both delegate to the unified _run_async_safe
from app.tasks (which performs reset_model_router + reset_engine and
dispose_current_engine_async).
"""
import asyncio
from unittest.mock import patch, MagicMock


def test_knowledge_tasks_run_async_delegates_to_run_async_safe():
    from app.tasks import knowledge_tasks

    sentinel = object()
    captured = {}

    def fake_safe(coro):
        captured['coro'] = coro
        return sentinel

    async def my_coro():
        return 'hi'

    co = my_coro()
    with patch('app.tasks._run_async_safe', side_effect=fake_safe):
        result = knowledge_tasks._run_async(co)
    assert result is sentinel, 'should return whatever _run_async_safe returns'
    assert captured['coro'] is co, 'should pass the same coroutine through'
    co.close()  # avoid 'never awaited' warning


def test_style_tasks_run_async_delegates_to_run_async_safe():
    from app.tasks import style_tasks

    sentinel = object()
    captured = {}

    def fake_safe(coro):
        captured['coro'] = coro
        return sentinel

    async def my_coro():
        return 42

    co = my_coro()
    with patch('app.tasks._run_async_safe', side_effect=fake_safe):
        result = style_tasks._run_async(co)
    assert result is sentinel
    assert captured['coro'] is co
    co.close()


def test_run_async_safe_calls_reset_engine_and_dispose():
    """Verify _run_async_safe invokes reset_engine before the new loop
    and dispose_current_engine_async in the finally block."""
    from app.tasks import _run_async_safe

    call_order = []

    def fake_reset_engine():
        call_order.append('reset_engine')

    async def fake_dispose():
        call_order.append('dispose')

    def fake_reset_router():
        call_order.append('reset_router')

    async def the_work():
        call_order.append('work')
        return 'done'

    with patch('app.db.session.reset_engine', side_effect=fake_reset_engine), \
         patch('app.db.session.dispose_current_engine_async', side_effect=fake_dispose), \
         patch('app.services.model_router.reset_model_router', side_effect=fake_reset_router):
        result = _run_async_safe(the_work())
    assert result == 'done'
    # router and engine reset before work, dispose after
    assert call_order.index('reset_router') < call_order.index('work')
    assert call_order.index('reset_engine') < call_order.index('work')
    assert call_order.index('work') < call_order.index('dispose')


def test_db_session_callable_proxy_picks_up_reset():
    """After reset_engine(), the next async_session_factory() call must
    build a fresh sessionmaker (the v1.13 callable-proxy contract that
    X2 relies on)."""
    from app.db import session as ses

    # Force-build initial state
    ses._build()
    sm1 = ses._state.sessionmaker
    assert sm1 is not None

    ses.reset_engine()
    assert ses._state.sessionmaker is None
    assert ses._state.engine is None

    # Calling the public proxy lazily rebuilds
    ses._build()
    sm2 = ses._state.sessionmaker
    assert sm2 is not None
    assert sm2 is not sm1, 'reset_engine + rebuild must produce a new sessionmaker'

    # Cleanup so other tests get a fresh state
    ses.reset_engine()
