import ast
import json
from pathlib import Path
from contextlib import aclosing
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', ['', '短。', '前场。\n\n未完成的后场'])
@pytest.mark.parametrize('context_failure', [False, True])
async def test_scene_error_branch_only_falls_back_before_any_prose(partial, context_failure):
    from app.services.context_errors import RequiredContextUnavailable
    tree = ast.parse((Path(__file__).parents[1] / 'app/api/generate.py').read_text())
    handler = next(n for n in ast.walk(tree) if isinstance(n,ast.ExceptHandler) and n.name=='scene_err')
    call = AsyncMock(return_value='单次候选。')
    async def heartbeat(coro): yield True, await coro
    chunks = [partial] if partial else []
    error = RequiredContextUnavailable('character_cognition_retrieval_unavailable') if context_failure else RuntimeError('failure')
    env=dict(aclosing=aclosing,json=json,scene_err=error,logger=MagicMock(),collected_text=chunks,_pending_scene_events=[],_run_single_shot_generator=call,_yield_with_heartbeat=heartbeat)
    code='async def exercise():\n    partial_draft_only = False\n    partial_quality_meta = None\n'
    for node in handler.body: code+='\n'.join('    '+line for line in ast.unparse(node).splitlines())+'\n'
    code+='    yield (partial_draft_only, partial_quality_meta)\n'
    exec(code,env)
    if context_failure and not partial:
        with pytest.raises(RequiredContextUnavailable):
            [v async for v in env['exercise']()]
        call.assert_not_awaited()
        return
    values=[v async for v in env['exercise']()]
    events=[json.loads(v.removeprefix('data: ')) for v in values[:-1]]
    if partial:
        call.assert_not_awaited()
        assert chunks==[partial] and values[-1]==(True,{'persisted_on_block':True,'reason':'partial_generation_interrupted'})
        assert not any(e.get('event')=='fallback_restart' or 'text' in e for e in events)
    else:
        call.assert_awaited_once()
        assert chunks==['单次候选。'] and values[-1][0] is False
