import asyncio
import ast
from pathlib import Path
import pytest
from app.api.generate import _yield_with_heartbeat


@pytest.mark.asyncio
async def test_heartbeat_close_cancels_and_joins_inner_work_immediately():
    state = []
    async def work():
        try:
            await asyncio.Event().wait()
        finally: state.append('cancelled')
    stream = _yield_with_heartbeat(work(), interval=.001)
    assert (await anext(stream))[0] is False
    await stream.aclose()
    assert state == ['cancelled']


@pytest.mark.asyncio
async def test_successful_heartbeat_retains_result_and_closes_cleanly():
    async def work(): return '原稿。'
    stream = _yield_with_heartbeat(work(), interval=1)
    assert await anext(stream) == (True, '原稿。')
    await stream.aclose()


def test_chapter_api_owns_heartbeat_and_generation_iterators():
    tree = ast.parse((Path(__file__).parents[1] / 'app/api/generate.py').read_text())
    prefixes = ('_yield_with_heartbeat(', 'revise_orchestrator.orchestrate_chapter_stream(')
    raw = [n for n in ast.walk(tree) if isinstance(n,ast.AsyncFor)
           and (ast.unparse(n.iter).startswith(prefixes) or ast.unparse(n.iter)=='stream_iter')]
    assert not raw
    owned = [n for n in ast.walk(tree) if isinstance(n,ast.AsyncWith)
             and any(ast.unparse(item.context_expr).startswith(('aclosing(_yield_with_heartbeat(', 'aclosing(stream_iter)', 'aclosing(revise_orchestrator.orchestrate_chapter_stream(')) for item in n.items)]
    assert len(owned) == 11  # Includes cognition preparation before judge/rewrite.
