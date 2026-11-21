"""Untrusted judge output must never masquerade as a usable quality verdict."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.auto_revise import should_revise
from app.services.chapter_evaluator import ChapterEvaluator, _build_user_prompt, _parse_evaluation_response
from app.services.model_router import GenerationResult, TokenUsage

DIMS = ('plot_coherence', 'character_consistency', 'style_adherence',
        'narrative_pacing', 'foreshadow_handling')


def payload():
    return {dim: {'score': 8, 'issues': []} for dim in DIMS}


@pytest.mark.parametrize('score', [True, False, None, '8', float('nan'), float('inf'), -1, 11])
def test_invalid_scores_are_not_clamped_into_valid_verdicts(score):
    data = payload()
    data[DIMS[0]]['score'] = score
    with pytest.raises(ValueError):
        _parse_evaluation_response(json.dumps(data))


@pytest.mark.parametrize('bad', [{}, [], {'plot_coherence': {'score': 8, 'issues': []}}])
def test_all_dimensions_required(bad):
    with pytest.raises(ValueError):
        _parse_evaluation_response(json.dumps(bad))


@pytest.mark.parametrize('issues', [None, {}, ['bad'], [{'quote': 123}], [{'paragraph': True}], [{'paragraph': -1}]])
@pytest.mark.parametrize('contract', [False, True])
def test_issue_schema_required(issues, contract):
    data = payload()
    if contract:
        data['contract_violations'] = issues
    else:
        data[DIMS[0]]['issues'] = issues
    with pytest.raises(ValueError):
        _parse_evaluation_response(json.dumps(data))


def test_unverifiable_quote_cannot_become_edit_anchor():
    data = payload()
    data[DIMS[0]]['issues'] = [{'quote': '作者从未写过这句话'}, {'quote': '守卫收下钱，却没有接信。'}]
    result = _parse_evaluation_response(json.dumps(data), chapter_text='守卫收下钱，却没有接信。门关了。')
    assert result.issues[0]['quote'] == ''
    assert result.issues[0]['quote_verified'] is False
    assert result.issues[1]['quote'] == '守卫收下钱，却没有接信。'
    assert result.issues[1]['quote_verified'] is True


def test_evaluator_receives_complete_middle_and_obligations():
    chapter = '甲' * 5000 + '仍在外舱的修理工操作了内舱门。' + '乙' * 5000
    outline = {'setup': '丙' * 3000, 'outcome': '本章必须把信交到收件人手中', 'tail': '丁' * 3000}
    ledger = '戊' * 1500 + '修理工不知道内舱密码' + '己' * 1500
    previous = '庚' * 1500 + '左手已截肢' + '辛' * 1500
    prompt = _build_user_prompt(chapter, outline, previous, '', [], ledger)
    assert chapter in prompt
    assert json.dumps(outline, ensure_ascii=False, indent=2) in prompt
    assert ledger in prompt
    assert previous in prompt
    assert '已截断' not in prompt


@pytest.mark.asyncio
async def test_invalid_schema_returns_untrusted_result_without_rewrite():
    router = AsyncMock()
    router.generate_with_tier_fallback.return_value = GenerationResult(
        text='{}', usage=TokenUsage(input_tokens=1, output_tokens=1), model='stub', provider='stub')
    with patch('app.services.chapter_evaluator.run_text_prompt', router.generate_with_tier_fallback):
        result = await ChapterEvaluator().evaluate('测试正文。', {})
    assert result.parse_failed
    assert not should_revise(result)


@pytest.mark.asyncio
async def test_empty_chapter_has_no_quality_verdict():
    result = await ChapterEvaluator().evaluate('  ', {})
    assert result.parse_failed
    assert not should_revise(result)


@pytest.mark.asyncio
async def test_evaluator_uses_registry_template_route_and_buffered_metrics():
    from uuid import uuid4
    from app.services.prompt_registry import RouteSpec
    from app.services import prompt_registry, prompt_cache
    route = RouteSpec(uuid4(), uuid4(), 'judge', 0.3, 2400,
                      '作者自定义评审要求：注意叙述视角。', 'structured')
    router = AsyncMock()
    router.generate_with_tier_fallback.return_value = GenerationResult(
        text=json.dumps(payload()), usage=TokenUsage(input_tokens=1, output_tokens=1),
        model='judge', provider='stub')
    with patch.object(prompt_registry, '_resolve_route_and_tier_cached', AsyncMock(return_value=(route, 'standard'))) as resolve, \
         patch.object(prompt_registry, 'get_model_router_async', AsyncMock(return_value=router)), \
         patch.object(prompt_cache, 'buffer_track_result', AsyncMock()) as track:
        result = await ChapterEvaluator().evaluate('守卫接过信。', {'outcome': '交信'})
    assert not result.parse_failed
    resolve.assert_awaited_once_with('chapter_evaluation', None)
    args, kwargs = router.generate_with_tier_fallback.await_args
    assert args[0] == 'chapter_evaluation'
    systems = '\n'.join(m['content'] for m in args[1] if m['role'] == 'system')
    assert route.system_prompt in systems
    assert 'chapter_completion_violation' in systems
    assert kwargs['route'] is route
    assert kwargs['preferred_tier'] == 'standard'
    assert kwargs['_log_meta']['prompt_id'] == route.prompt_id
    track.assert_awaited_once_with(route.prompt_id, success=True)
