"""Actual generation/review entry points must finish deferred recall first."""
import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app
from app.api import quality, versions
from app.services import chapter_generator as single, scene_orchestrator as scene, cognition_retrieval as retrieval
from app.services.chapter_evaluator import _build_user_prompt
from app.services.context_errors import RequiredContextUnavailable
from app.services.context_pack import ContextPack
from app.services.evaluation_neighbors import EvaluationNeighbors
from app.services.generation_errors import generation_error_payload, review_context_message
from tests.test_cognition_retrieval import prompt, FixedSpace, vector_db, sessions_for


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['single', 'stream', 'scene'])
@pytest.mark.parametrize('fails', [False, True])
async def test_generation_resolves_after_collection_and_before_any_model(monkeypatch, vector_db, mode, fails):
    request = prompt()
    pack = ContextPack(cognition_boundaries=request)
    opened = False
    @asynccontextmanager
    async def context(db):
        nonlocal opened
        opened = True
        try: yield NS()
        finally: opened = False
    module = scene if mode == 'scene' else single
    monkeypatch.setattr(module, 'generation_context_session', context)
    monkeypatch.setattr(module, 'ContextPackBuilder', lambda **kwargs: NS(build=AsyncMock(return_value=pack)))
    service = scene.SceneOrchestrator() if mode == 'scene' else single.ChapterGenerator()
    monkeypatch.setattr(service, '_assert_outline_chain_ready', AsyncMock())
    monkeypatch.setattr(single, '_check_character_names_in_text', AsyncMock(return_value=[]))
    space = FixedSpace()
    if fails: space.embed = AsyncMock(side_effect=RuntimeError('encoder unavailable'))
    real_resolve = retrieval.resolve_prompt_context
    rechecks = sessions_for(request.ledger, monkeypatch)
    async def resolve(value, **kwargs):
        assert not opened and kwargs['extra_context'] == '让河岸旧事在本章产生影响。'
        return await real_resolve(value, client=vector_db, sessions=rechecks,
            space_loader=AsyncMock(return_value=space), **kwargs)
    monkeypatch.setattr(retrieval, 'resolve_prompt_context', resolve)
    model_calls = []
    async def model(**kwargs):
        assert not opened and kwargs['db'] is None
        assert isinstance(pack.cognition_boundaries, str)
        if mode == 'scene': assert pack.cognition_boundaries in kwargs['pack'].to_system_prompt()
        else: assert any(pack.cognition_boundaries in msg['content'] for msg in kwargs['messages'])
        model_calls.append(kwargs)
        return [] if mode == 'scene' else NS(text='本章正文。', finish_reason='stop')
    async def stream(**kwargs): yield (await model(**kwargs)).text
    if mode == 'scene': monkeypatch.setattr(service, 'plan_scenes', model)
    else:
        monkeypatch.setattr(single, 'run_text_prompt', model)
        monkeypatch.setattr(single, 'stream_text_prompt', stream)
    async def drive():
        args = dict(project_id=request.project_id, volume_id='v', chapter_idx=2, db=None,
                    user_instruction='让河岸旧事在本章产生影响。')
        if mode == 'single': return await service.generate(**args)
        iterator = service.generate_stream(**args) if mode == 'stream' else service.orchestrate_chapter_stream(**args)
        return [chunk async for chunk in iterator]
    if fails:
        with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'): await drive()
        assert not model_calls
    else:
        await drive()
        assert len(model_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_quality_api_ends_request_transaction_before_resolution(monkeypatch, fails):
    from app.models.project import Chapter
    request = prompt()
    pack = ContextPack(cognition_boundaries=request)
    transaction = True
    chapter = NS(content_text='正文。', volume_id='v', chapter_idx=2)
    async def rollback():
        nonlocal transaction
        transaction = False
    db = NS(get=AsyncMock(side_effect=lambda model, key: chapter if model is Chapter else NS(project_id=request.project_id)),
            rollback=AsyncMock(side_effect=rollback), in_transaction=lambda: transaction)
    monkeypatch.setattr('app.services.context_pack.ContextPackBuilder',
        lambda: NS(build=AsyncMock(return_value=pack), close=AsyncMock()))
    async def resolve(value, **kwargs):
        assert not transaction and value is request
        if fails: raise RequiredContextUnavailable('character_cognition_retrieval_unavailable')
        return '作者确认的认知原句。'
    monkeypatch.setattr(retrieval, 'resolve_prompt_context', resolve)
    async def check(text, context):
        assert not transaction and context.cognition_boundaries == '作者确认的认知原句。'
        from app.services.checkers.checker_manager import AggregatedResult
        return AggregatedResult(overall_score=8, passed=True, completed=True)
    check_call = AsyncMock(side_effect=check)
    monkeypatch.setattr('app.services.checkers.checker_manager.CheckerManager', lambda: NS(run_all=check_call))
    if fails:
        with pytest.raises(HTTPException) as caught: await quality.check_quality('c', None, db)
        assert caught.value.status_code == 503 and '嵌入' in caught.value.detail
        check_call.assert_not_awaited()
    else:
        assert (await quality.check_quality('c', None, db))['passed'] is True
        check_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_evaluation_retrieval_failure_is_explicit_and_does_not_score(monkeypatch):
    request = prompt()
    chapter = NS(id='c', content_text='正文。', outline_json={}, volume_id='v')
    monkeypatch.setattr(versions, '_get_chapter_or_404', AsyncMock(return_value=chapter))
    monkeypatch.setattr('app.services.character_cognition.load_prompt_context', AsyncMock(return_value=request))
    monkeypatch.setattr('app.services.evaluation_neighbors.load_evaluation_neighbors', AsyncMock(return_value=EvaluationNeighbors('', '')))
    db = NS(get=AsyncMock(return_value=NS(project_id=request.project_id)), rollback=AsyncMock())
    async def unavailable(value, **kwargs):
        db.rollback.assert_awaited_once()
        assert value is request
        raise RequiredContextUnavailable('character_cognition_retrieval_unavailable')
    monkeypatch.setattr(retrieval, 'resolve_prompt_context', unavailable)
    model = AsyncMock()
    monkeypatch.setattr(versions.ChapterEvaluator, 'evaluate', model)
    with pytest.raises(HTTPException) as caught: await versions.evaluate_chapter('c', versions.EvaluateRequest(), db)
    assert caught.value.status_code == 503 and '嵌入' in caught.value.detail and '补提取' not in caught.value.detail
    model.assert_not_awaited()


def test_unresolved_review_and_retrieval_failure_cannot_look_complete():
    with pytest.raises(RequiredContextUnavailable, match='pending'):
        _build_user_prompt('正文', {}, '', '', None, cognition_ledger_text=prompt())
    error = generation_error_payload(RequiredContextUnavailable('character_cognition_retrieval_unavailable'), 'chapter')
    assert error['code'] == 'chapter_context_unavailable' and '嵌入' in error['error'] and '补提取' not in error['error']
    assert '人物认知' not in review_context_message(RuntimeError('private connection detail'))
    assert 'private' not in review_context_message(RuntimeError('private connection detail'))


def test_revision_attempts_prepare_the_current_candidate_inside_each_round():
    root = Path(app.__file__).parent
    for path in ('api/generate.py', 'tasks/generation_tasks.py'):
        tree = ast.parse((root / path).read_text())
        rounds = [node for node in ast.walk(tree) if isinstance(node, ast.For)
                  and getattr(node.target, 'id', None) == 'round_idx']
        calls = [node for loop in rounds for node in ast.walk(loop) if isinstance(node, ast.Call)
                 and getattr(node.func, 'id', None) == 'load_candidate_context']
        assert len(calls) == 2, (path, 'evaluation and targeted rewrite must each refresh')
        for call in calls:
            value = next(kw.value for kw in call.keywords if kw.arg == 'chapter_text')
            assert isinstance(value, ast.Name) and value.id == 'current_text'


def test_worker_generation_calls_use_detached_context_sessions():
    # The task is a large resumable worker. Pin its transaction ownership at
    # all actual chapter-service calls; service behavior is exercised above.
    root = Path(app.__file__).parent
    tree = ast.parse((root / 'tasks/generation_tasks.py').read_text())
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute): continue
        if node.func.attr == 'orchestrate_chapter_stream' or (
            node.func.attr == 'generate' and isinstance(node.func.value, ast.Call)
            and getattr(node.func.value.func, 'id', None) == 'ChapterGenerator'
        ): calls.append(node)
    assert len(calls) == 6
    for call in calls:
        db = next(kw.value for kw in call.keywords if kw.arg == 'db')
        assert isinstance(db, ast.Constant) and db.value is None


@pytest.mark.asyncio
async def test_manual_consistency_checker_receives_full_target_and_cognition(monkeypatch):
    from app.services.checkers.consistency_checker import ConsistencyChecker
    body = '河边人家的日常。' * 700 + '尾部独有的转折。'
    cognition = '先前获知的消息及完整引文。'
    call = AsyncMock(return_value=NS(text='[]', finish_reason='stop'))
    monkeypatch.setattr('app.services.model_router.get_model_router_async',
        AsyncMock(return_value=NS(generate_with_tier_fallback=call)))
    await ConsistencyChecker().check(body, ContextPack(cognition_boundaries=cognition))
    messages = call.await_args.kwargs['messages']
    assert body in messages[-1]['content'] and cognition in messages[-1]['content']
    assert '不可靠叙述' in messages[0]['content']
