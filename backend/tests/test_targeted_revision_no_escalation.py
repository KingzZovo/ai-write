"""Execute the production decision branches without external DB/model fixtures.

These are branch-level tests, not full ASGI/Celery integration tests.
"""
from contextlib import aclosing
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.auto_revise import SpanRevisionResult, RevisionContext
from app.services.context_errors import RequiredContextUnavailable


def production_branch(relative):
    tree = ast.parse((Path(__file__).parents[1] / relative).read_text())
    found = [node for node in ast.walk(tree) if isinstance(node, ast.If)
             and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Name)
             and node.test.func.id == 'targeted_revision_enabled']
    assert len(found) == 1
    return ast.unparse(found[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['sse', 'worker'])
@pytest.mark.parametrize('outcome', ['unlocated', 'empty', 'error', 'success', 'disabled', 'context_error'])
async def test_targeted_failure_does_not_escalate_to_full_rewrite(route, outcome):
    source = 'app/api/generate.py' if route == 'sse' else 'app/tasks/generation_tasks.py'
    branch = production_branch(source)
    events, full_calls, metrics = [], [], []
    current = '当前完整候选正文。'
    issue = {'quote': '不存在的引文', 'description': '需要人工核查'}
    result = SpanRevisionResult(text=current if outcome != 'success' else '局部修订后的候选正文。',
                                spans_revised=1 if outcome == 'success' else 0,
                                unlocatable_issues=[issue] if outcome == 'unlocated' else [])
    revise = AsyncMock(return_value=result, side_effect=RuntimeError('model unavailable') if outcome == 'error' else None)
    prepare = AsyncMock(return_value='尚不知道密码', side_effect=(
        RequiredContextUnavailable('character_cognition_retrieval_unavailable') if outcome == 'context_error' else None))
    async def heartbeat(coro): yield True, await coro
    task = SimpleNamespace(params_json={'keep': 'existing'})
    db = SimpleNamespace(commit=AsyncMock())
    env = dict(aclosing=aclosing, targeted_revision_enabled=lambda: outcome != 'disabled', revise_spans=revise,
               _yield_with_heartbeat=heartbeat, req=SimpleNamespace(project_id='p', chapter_id='c'),
               eval_result=SimpleNamespace(issues=[issue]), project_id='p', chapter_id_for_eval='c',
               logger=MagicMock(), task=task, db=db, json=json, _x4_inc_revise=metrics.append,
               full_calls=full_calls)
    env.update(load_candidate_context=prepare, session_factory=None, read_session_factory=None,
               RequiredContextUnavailable=RequiredContextUnavailable)
    env.update(RevisionContext=RevisionContext, effective_user_instruction='作者要求',
               generated_chapter_user_instr='作者要求', revise_outline={'goal': '送还物件'},
               generated_chapter_outline={'goal': '送还物件'}, evaluation_context='前章事实',
               prev_context='前章事实', following_context_for_eval='后章参照',
               neighbors=SimpleNamespace(following_context='后章参照'), resolved_style='克制',
               style_text='克制', cognition_ledger_text='尚不知道密码')
    lines = ['async def exercise(current_text):' , '    targeted_text = None', '    for round_idx in range(1, 2):']
    lines.extend('        ' + line for line in branch.splitlines())
    if route == 'sse':
        lines += ['        if targeted_text is None:', '            full_calls.append(current_text)']
    else:
        lines += ['        full_calls.append(current_text)']
    exec('\n'.join(lines), env)
    if outcome == 'context_error':
        if route == 'sse':
            with pytest.raises(RequiredContextUnavailable):
                [event async for event in env['exercise'](current)]
        else:
            await env['exercise'](current)
        prepare.assert_awaited_once()
        revise.assert_not_awaited()
        assert not full_calls
        return
    if route == 'sse':
        async for event in env['exercise'](current): events.append(json.loads(event.removeprefix('data: ')))
    else:
        await env['exercise'](current)
    if outcome == 'disabled':
        prepare.assert_not_awaited()
        revise.assert_not_awaited()
        assert full_calls == [current]
    else:
        assert prepare.await_args.kwargs['chapter_text'] == current
        revise.assert_awaited_once()
        assert revise.call_args.kwargs['context'] == RevisionContext(
            user_instruction='作者要求', chapter_outline={'goal': '送还物件'},
            previous_context='前章事实', following_context='后章参照',
            style_profile='克制', cognition_ledger='尚不知道密码')
        assert full_calls == []
    if outcome in ('unlocated', 'empty', 'error'):
        if route == 'sse':
            assert events[-1]['event'] == 'revise_skipped'
            assert events[-1]['reason'] == 'targeted_revision_unapplied'
        else:
            assert task.params_json == {'keep': 'existing', 'revision_stop_reason': 'targeted_revision_unapplied'}
            db.commit.assert_awaited_once()
    elif outcome == 'success' and route == 'sse':
        assert events[-1]['event'] == 'targeted_revision'
