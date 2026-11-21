import ast
import asyncio
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest

SOURCE = Path(__file__).parents[1] / 'app/tasks/generation_tasks.py'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['save_error', 'cancel'])
async def test_actual_worker_consumer_closes_scene_stream_on_save_failure_or_cancel(outcome):
    tree = ast.parse(SOURCE.read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_consume_scene_stream']
    assert len(nodes)==1
    closed, chunks = [], []
    entered = asyncio.Event()
    class Orchestrator:
        async def orchestrate_chapter_stream(self, **kw):
            try:
                yield '恢复片段。'
                await asyncio.Event().wait()
            finally: closed.append(True)
    async def write(*args):
        entered.set()
        if outcome == 'save_error': raise RuntimeError('commit failed')
        await asyncio.Event().wait()
    env = dict(aclosing=aclosing,orchestrator=Orchestrator(),project_id='p',generated_chapter_volume_id_safe='v',generated_chapter_idx_safe=1,db=NS(commit=AsyncMock()),generated_chapter_id_safe='c',user_instr='要求',generated_chapter_target_words=1000,_append_chunk=chunks.append,progress_writer=NS(write=write),task=NS(),_joined_collected=lambda: ''.join(chunks))
    exec(ast.unparse(nodes[0]),env)
    task = asyncio.create_task(env['_consume_scene_stream']())
    await asyncio.wait_for(entered.wait(),1)
    if outcome == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        with pytest.raises(RuntimeError,match='commit failed'): await task
    assert closed == [True] and chunks == ['恢复片段。']


def test_all_worker_streams_have_explicit_ownership():
    tree = ast.parse(SOURCE.read_text())
    loops = [n for n in ast.walk(tree) if isinstance(n,ast.AsyncFor)]
    assert len(loops)==4 and all(ast.unparse(n.iter)=='owned_stream' for n in loops)


@pytest.mark.asyncio
async def test_partial_initial_generation_stops_before_fallback_and_preserves_text():
    from unittest.mock import AsyncMock
    tree = ast.parse(SOURCE.read_text())
    branches = [n for n in ast.walk(tree) if isinstance(n,ast.If) and 'last_scene_err is not None' in ast.unparse(n.test) and 'any(collected)' in ast.unparse(n.test)]
    assert len(branches)==1
    task = NS(params_json={'existing':1})
    db = NS(commit=AsyncMock())
    env = dict(_force_direct=False,last_scene_err=RuntimeError('failed'),collected=['  残稿。\n'],task=task,db=db,_joined_collected=lambda:'  残稿。\n')
    code = 'async def exercise():\n'+'\n'.join('    '+line for line in ast.unparse(branches[0]).splitlines())+'\n    raise AssertionError("fallback reached")\n'
    exec(code,env)
    await env['exercise']()
    assert task.status=='needs_review' and task.result_text==task.progress_text=='  残稿。\n'
    assert task.params_json['generation_stop_reason']=='partial_generation_interrupted'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [RuntimeError, TimeoutError])
async def test_initial_worker_retry_loop_does_not_restart_after_prose(error_type):
    from unittest.mock import AsyncMock, MagicMock
    tree = ast.parse(SOURCE.read_text())
    loops = [n for n in ast.walk(tree) if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='_attempt' and '_consume_scene_stream' in ast.unparse(n)]
    assert len(loops)==1
    calls, chunks = [], []
    class Orchestrator:
        async def orchestrate_chapter_stream(self, **kw):
            calls.append(True)
            yield '第一次的残稿。'
            raise error_type('interrupted')
    env = dict(aclosing=aclosing,SceneOrchestrator=Orchestrator,_asyncio=asyncio,_scene_timeout=2,project_id='p',generated_chapter_volume_id_safe='v',generated_chapter_idx_safe=1,generated_chapter_id_safe='c',generated_chapter_target_words=1000,user_instr='',db=NS(commit=AsyncMock()),task=NS(),task_id='t',logger=MagicMock(),collected=chunks,_append_chunk=chunks.append,_joined_collected=lambda:''.join(chunks),progress_writer=NS(write=AsyncMock()))
    exec('async def exercise():\n'+'\n'.join('    '+line for line in ast.unparse(loops[0]).splitlines())+'\n    return last_scene_err\n',env)
    assert isinstance(await env['exercise'](),error_type)
    assert calls==[True] and chunks==['第一次的残稿。']
