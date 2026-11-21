"""Chapter registration must not reuse the reference scorer's schema."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from sqlalchemy import select
from tests.test_pipeline_control import runtime_db
from tests.test_prompt_cache_transactions import tables
from app.models.project import LLMEndpoint
from app.models.prompt import PromptAsset
from app.services import prompt_cache, prompt_registry
from app.services.chapter_evaluator import ChapterEvaluator
from app.services.model_router import _resolve_nonstream_timeout


@pytest.mark.asyncio
async def test_seed_inherits_route_not_generic_prompt_and_preserves_customization(runtime_db, monkeypatch):
    r = runtime_db
    await tables(r)
    prompt_cache.reset_for_tests()
    async with r.sessions() as db:
        endpoint = LLMEndpoint(name='judge', provider_type='openai', default_model='test')
        db.add(endpoint)
        await db.flush()
        parent = PromptAsset(task_type='evaluation', name='reference', system_prompt='只评学习价值',
                             endpoint_id=endpoint.id, model_name='judge-model', model_tier='flagship',
                             max_tokens=3000, temperature=.2)
        db.add(parent)
        await db.commit()
        await prompt_registry.PromptRegistry(db).seed_builtins()
        await db.commit()
        child = await db.scalar(select(PromptAsset).where(PromptAsset.task_type == 'chapter_evaluation'))
        assert child.endpoint_id == parent.endpoint_id
        assert child.model_name == 'judge-model'
        assert child.max_tokens == 3000 and child.model_tier == 'flagship'
        dimensions = set(child.output_schema)
        assert dimensions == {'plot_coherence', 'character_consistency', 'style_adherence', 'narrative_pacing', 'foreshadow_handling'}
        child.system_prompt = '作者章节评审要求'
        await db.commit()
        assert await prompt_registry.PromptRegistry(db).seed_builtins() == 0
        assert child.system_prompt == '作者章节评审要求'
        assert parent.system_prompt == '只评学习价值'
    opened = []
    @asynccontextmanager
    async def factory():
        opened.append(True)
        try:
            async with r.sessions() as db:
                yield db
        finally:
            opened.pop()
    monkeypatch.setattr('app.db.session.async_session_factory', factory)
    async def model(task, messages, **kwargs):
        assert not opened
        assert task == 'chapter_evaluation'
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        assert '作者章节评审要求' in system and '只评学习价值' not in system
        assert 'chapter_completion_violation' in system
        assert kwargs['route'].model == 'judge-model'
        effective_budget = kwargs.get('max_tokens')
        if effective_budget is None:
            effective_budget = kwargs['route'].max_tokens
        assert effective_budget == 3000, 'chapter judge must honor its configured output budget'
        return SimpleNamespace(text=json.dumps({d: {'score': 8, 'issues': []} for d in dimensions}),
                               usage=SimpleNamespace(total_tokens=1))
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', AsyncMock(return_value=SimpleNamespace(generate_with_tier_fallback=model)))
    result = await ChapterEvaluator().evaluate('正文。', {})
    assert result.overall == 8 and not result.parse_failed
    prompt_cache.reset_for_tests()


def test_chapter_judge_has_evaluation_timeout(monkeypatch):
    monkeypatch.setenv('EVALUATION_REQUEST_TIMEOUT', '135')
    assert _resolve_nonstream_timeout(None, 'chapter_evaluation') == 135
    assert _resolve_nonstream_timeout(12, 'chapter_evaluation') == 12


@pytest.mark.asyncio
async def test_missing_chapter_binding_never_falls_back_to_reference_scorer(runtime_db, monkeypatch):
    r = runtime_db
    await tables(r)
    prompt_cache.reset_for_tests()
    async with r.sessions() as db:
        endpoint = LLMEndpoint(name='reference-only', provider_type='openai', default_model='test')
        db.add(endpoint)
        await db.flush()
        db.add(PromptAsset(task_type='evaluation', name='reference', system_prompt='通用评分', endpoint_id=endpoint.id))
        await db.commit()
    monkeypatch.setattr('app.db.session.async_session_factory', r.sessions)
    router = AsyncMock()
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', router)
    result = await ChapterEvaluator().evaluate('正文。', {})
    assert result.parse_failed
    # Refreshing committed configuration is allowed; a missing chapter binding
    # must still fail without dispatching to any model or generic scorer.
    router.return_value.generate_with_tier_fallback.assert_not_called()
    router.return_value.stream_with_tier_fallback.assert_not_called()
    prompt_cache.reset_for_tests()
