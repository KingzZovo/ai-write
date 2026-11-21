from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.services import prompt_registry, auto_revise


@pytest.mark.asyncio
async def test_targeted_revision_uses_registered_model_and_attribution(monkeypatch):
    route = prompt_registry.RouteSpec(None, None, 'configured-writer', 0.3, 8192,
                                      '作者配置的修订风格', 'text')
    captured = []
    async def generate(task_type, messages, **kwargs):
        captured.append((task_type, messages, kwargs))
        return SimpleNamespace(text='林禾把家谱翻到那一页。')
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', AsyncMock(return_value=SimpleNamespace(generate_with_tier_fallback=generate)))
    monkeypatch.setattr(prompt_registry, '_resolve_route_and_tier_cached', AsyncMock(return_value=(route, 'T1')))
    result = await auto_revise.revise_spans('林禾把家谱放到桌上。', [{'quote': '放到桌上', 'description': '应当翻到指定页'}],
                                           project_id='book-id', chapter_id='chapter-id')
    assert result.text == '林禾把家谱翻到那一页。'
    task, messages, kwargs = captured[0]
    assert task == 'rewrite' and kwargs['route'] is route
    assert kwargs['_log_meta']['project_id'] == 'book-id'
    assert kwargs['_log_meta']['chapter_id'] == 'chapter-id'
    assert '作者配置的修订风格' in messages[0]['content']
    assert auto_revise.SPAN_REWRITE_SYSTEM_PROMPT in messages[0]['content']


@pytest.mark.asyncio
async def test_evaluator_threads_identity_to_registry(monkeypatch):
    from app.services import chapter_evaluator
    call = AsyncMock(return_value=SimpleNamespace(text='{"overall":9,"issues":[]}', usage=SimpleNamespace(total_tokens=0)))
    monkeypatch.setattr(chapter_evaluator, 'run_text_prompt', call)
    await chapter_evaluator.ChapterEvaluator().evaluate('林禾合上家谱。', {}, project_id='book-id', chapter_id='chapter-id')
    assert call.call_args.kwargs['project_id'] == 'book-id'
    assert call.call_args.kwargs['chapter_id'] == 'chapter-id'
