from contextlib import asynccontextmanager, aclosing
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services import chapter_generator as module
from app.services.model_router import GenerationResult, TokenUsage


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter', 'tool_calls', 'unknown', 'stop', 'end_turn', None])
async def test_single_shot_checks_completion_before_returning_prose(monkeypatch, reason):
    @asynccontextmanager
    async def context(db): yield db
    monkeypatch.setattr(module, 'generation_context_session', context)
    pack = NS(to_messages=lambda instruction: [{'role': 'user', 'content': instruction}],
              resolve_cognition=AsyncMock())
    monkeypatch.setattr(module, 'ContextPackBuilder', lambda **kw: NS(build=AsyncMock(return_value=pack)))
    monkeypatch.setattr(module.ChapterGenerator, '_assert_outline_chain_ready', AsyncMock())
    monkeypatch.setattr(module.ChapterGenerator, '_collect_rag_hits', lambda self, pack: [])
    check = AsyncMock(return_value=[])
    monkeypatch.setattr(module, '_check_character_names_in_text', check)
    call = AsyncMock(return_value=GenerationResult('看似完整的半篇正文。', TokenUsage(), 'test', 'test', reason))
    monkeypatch.setattr(module, 'run_text_prompt', call)
    kwargs = dict(project_id='p', volume_id='v', chapter_idx=1, db=None,
                  user_instruction='归还物件，完成道歉。', stream=True, request_timeout=120, retry_attempts=1)
    if reason in ('stop', 'end_turn', None):
        assert await module.ChapterGenerator().generate(**kwargs) == '看似完整的半篇正文。'
        check.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match='^provider_incomplete_response$'):
            await module.ChapterGenerator().generate(**kwargs)
        check.assert_not_awaited()
    call.assert_awaited_once()
    assert call.call_args.kwargs['stream'] is True
    assert call.call_args.kwargs['request_timeout'] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', [True, False])
@pytest.mark.parametrize('context_failure', [False, True])
async def test_worker_full_rewrite_never_retries_after_partial_delivery(partial, context_failure):
    """Execute the actual worker retry loop; not a full Celery/DB test."""
    import ast
    from pathlib import Path
    from app.services.context_errors import RequiredContextUnavailable
    tree = ast.parse((Path(__file__).parents[1] / 'app/tasks/generation_tasks.py').read_text())
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == '_attempt'
             and 'regen_chunks.append' in ast.unparse(n)]
    assert len(loops) == 1
    calls = []
    class Orchestrator:
        async def orchestrate_chapter_stream(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                if partial: yield '未完成的改写开头。'
                if context_failure: raise RequiredContextUnavailable('character_cognition_retrieval_unavailable')
                raise ValueError('provider_incomplete_response')
            yield '第二次完整候选。'
    env = dict(aclosing=aclosing, SceneOrchestrator=Orchestrator, project_id='p', volume_id_for_eval='v',
               chapter_idx_for_eval=1, chapter_id_for_eval='c', merged_instruction='作者要求',
               generated_chapter_target_words=1000, db=NS(commit=AsyncMock()),
               task=NS(), task_id='t', logger=MagicMock(), _asyncio=NS(sleep=AsyncMock()))
    env['RequiredContextUnavailable'] = RequiredContextUnavailable
    source = 'async def exercise():\n    regen_chunks = []\n    last_regen_err = None\n'
    source += '\n'.join('    ' + line for line in ast.unparse(loops[0]).splitlines())
    source += '\n    return regen_chunks, last_regen_err\n'
    exec(source, env)
    chunks, error = await env['exercise']()
    if partial or context_failure:
        assert len(calls) == 1 and chunks == (['未完成的改写开头。'] if partial else [])
        assert isinstance(error, RequiredContextUnavailable if context_failure else ValueError)
        env['_asyncio'].sleep.assert_not_awaited()
    else:
        assert len(calls) == 2 and chunks == ['第二次完整候选。'] and error is None
