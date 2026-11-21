"""A missing review must never look like a clean manuscript."""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services import critic_service, generation_runner
from app.services.bvsr import VariantCandidate, rank
from app.services.checkers.base import BaseChecker, CheckResult
from app.services.checkers.checker_manager import CheckerManager
from app.services.checkers.consistency_checker import ConsistencyChecker
from app.services.context_pack import ContextPack
from tests.support.generation_run_fixture import verified_history


DRAFT = '沈青把两千元放进信封，推到弟弟面前。'


def review(draft=DRAFT, issues=None, *, status='complete'):
    items = issues or []
    return {
        'review_version': 1,
        'review_status': status,
        'draft_sha256': hashlib.sha256(draft.encode()).hexdigest(),
        'stages': [{'name': 'critic', 'status': status}],
        'issues': items,
        'observations': [],
        'hard_count': sum(i['severity'] == 'hard' for i in items),
        'soft_count': sum(i['severity'] == 'soft' for i in items),
        'info_count': sum(i['severity'] == 'info' for i in items),
    }


HARD = {
    'severity': 'hard', 'category': 'resource_continuity',
    'desc': '交付的金额与前文明确约定不符。', 'location': '两千元',
    'suggestion': '按作者约定核对金额。', 'source': 'llm',
}


@pytest.fixture
def critic_io(monkeypatch):
    monkeypatch.setattr(critic_service, 'scan_anti_ai', AsyncMock(return_value=[]))
    async def no_driver():
        yield None
    monkeypatch.setattr('app.db.neo4j.get_neo4j', no_driver)
    for module in ['time_reversal', 'geo_jump', 'item_missing']:
        monkeypatch.setattr(f'app.services.checkers.{module}.scan_{module}', AsyncMock(return_value=[]))
    monkeypatch.setenv('CRITIC_SPLIT_ENABLED', '1')
    monkeypatch.setenv('CRITIC_CONSISTENCY_LLM_ENABLED', '0')

    def install(response):
        async def structured(task, *_args, **_kwargs):
            value = response(task)
            if isinstance(value, Exception):
                raise value
            return value
        async def text(task, *_args, **_kwargs):
            return NS(text=json.dumps(await structured(task), ensure_ascii=False), finish_reason='stop')
        monkeypatch.setattr(critic_service, 'run_structured_prompt', structured, raising=False)
        monkeypatch.setattr(critic_service, 'run_text_prompt', text, raising=False)
    return install


@pytest.mark.asyncio
async def test_one_failed_critic_stream_cannot_be_a_complete_review(critic_io):
    critic_io(lambda task: {'issues': [HARD]} if task == 'critic_hard' else RuntimeError('private endpoint'))
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result.get('review_status') == 'incomplete'
    assert result['hard_count'] == 1
    assert 'private endpoint' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [{}, {'issues': None}, {'issues': [None]}, {'issues': [{'severity': 'hard'}]}])
async def test_invalid_critic_output_is_not_zero_issues(critic_io, bad):
    critic_io(lambda _task: bad)
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result.get('review_status') == 'incomplete'


def test_unreviewed_variant_cannot_beat_a_reviewed_candidate():
    unchecked = VariantCandidate(0, DRAFT, {})
    checked = VariantCandidate(1, DRAFT, review(issues=[HARD]))
    assert rank([unchecked, checked]) == [checked]
    assert unchecked.score is None


def test_review_for_another_draft_cannot_enter_ranking():
    stale = VariantCandidate(0, DRAFT + '她又抽回五百元。', review())
    assert rank([stale]) == []


class BrokenChecker(BaseChecker):
    name = 'consistency'
    async def check(self, chapter_text, context):
        raise RuntimeError('private endpoint secret')


@pytest.mark.asyncio
async def test_checker_failure_is_incomplete_not_a_neutral_literary_score():
    result = await CheckerManager(checkers=[BrokenChecker()]).run_all(DRAFT, ContextPack())
    assert result.passed is False
    assert result.overall_score is None
    data = result.to_dict()
    assert data['review_status'] == 'incomplete'
    assert data['total_issues'] == 0
    assert 'private endpoint secret' not in json.dumps(data)


@pytest.mark.asyncio
async def test_no_registered_checkers_cannot_report_pass():
    result = await CheckerManager(checkers=[]).run_all(DRAFT, ContextPack())
    assert result.passed is False
    assert result.overall_score is None


@pytest.mark.asyncio
async def test_unmentioned_location_is_reasonable_narrative_omission(critic_io):
    critic_io(lambda _: {'issues': []})
    result = await critic_service.run_critic(
        draft='沈青将信封推到弟弟面前。', project_id='p', chapter_id='c',
        db=NS(), pack_summary='沈青当前所在省城，上一场已经交代。',
    )
    assert result['review_status'] == 'complete' and result['issues'] == []


@pytest.mark.asyncio
async def test_repeating_a_prohibition_does_not_mean_breaking_it(monkeypatch):
    router = NS(generate_with_tier_fallback=AsyncMock(return_value=NS(text='[]', finish_reason='stop')))
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=router))
    result = await ConsistencyChecker().check(
        '此地不能飞行。沈青收起法器，沿着石阶往上走。',
        ContextPack(world_rules=['此地不能飞行。']),
    )
    assert result.passed is True
    assert result.issues == []


def run_at_critic(*, rewrites=0, limit=0, report=None):
    run = NS(
        id=uuid.uuid4(), project_id=uuid.uuid4(), chapter_id=uuid.uuid4(),
        phase='critic', status='running', rewrite_count=rewrites,
        max_rewrite_count=limit, last_error='',
        checkpoint_data={'planning': {'pack': {'system_prompt': '已核对的前文'}},
                         'drafting': {'text': DRAFT},
                         **({'critic': {'report': report, 'round': 1}} if report else {})},
    )
    verified_history(run)
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize('report', [review(issues=[HARD]), review(status='incomplete')])
async def test_generation_cannot_finalize_unresolved_or_missing_review(monkeypatch, report):
    run = run_at_critic()
    db = NS(get=AsyncMock(return_value=run), commit=AsyncMock(), add=lambda row: None)
    monkeypatch.setattr(generation_runner, '_phase_critic', AsyncMock(return_value=report))
    monkeypatch.setattr(generation_runner.run_bus, 'publish', AsyncMock())
    result = await generation_runner._execute_run_locked(run_id=str(run.id), db=db)
    assert result['status'] == 'needs_review'
    assert result['final_text'] == DRAFT
    assert 'finalize' not in run.checkpoint_data
    assert not any(c.kwargs['event'] == 'run.done' for c in generation_runner.run_bus.publish.await_args_list)


@pytest.mark.asyncio
async def test_empty_list_from_all_finished_streams_is_a_complete_review(critic_io):
    from app.services.review_contract import critic_review_status
    critic_io(lambda _: {'issues': []})
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert critic_review_status(result, DRAFT) == 'complete'
    assert result['issues'] == []
    assert result['editorial_status'] == 'needs_author_review'


@pytest.mark.asyncio
async def test_single_configured_critic_does_not_require_split_streams(critic_io, monkeypatch):
    from app.services.review_contract import critic_review_status
    calls = []
    def response(task):
        calls.append(task)
        return {'issues': []}
    critic_io(response)
    monkeypatch.setenv('CRITIC_SPLIT_ENABLED', '0')
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert calls == ['critic']
    assert critic_review_status(result, DRAFT) == 'complete'


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls', None])
async def test_even_valid_empty_json_requires_a_normal_finish(critic_io, monkeypatch, finish):
    monkeypatch.setattr(critic_service, 'run_text_prompt', AsyncMock(return_value=NS(text='{"issues": []}', finish_reason=finish)))
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result['review_status'] == 'incomplete'


@pytest.mark.asyncio
async def test_hallucinated_evidence_does_not_trigger_rewrite(critic_io):
    critic_io(lambda _: {'issues': [{**HARD, 'location': '原文不存在的三千元'}]})
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result['review_status'] == 'incomplete'
    assert result['hard_count'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', [False, True, 'false', None])
async def test_binary_consistency_verdict_is_applied_or_marked_incomplete(critic_io, monkeypatch, verdict):
    def response(task):
        if task == 'critic_hard': return {'issues': [HARD]}
        if task == 'critic_soft': return {'issues': []}
        return {'is_issue': verdict, 'severity': 'hard', 'reason': '结合交付动作核对原句。'}
    critic_io(response)
    monkeypatch.setenv('CRITIC_CONSISTENCY_LLM_ENABLED', '1')
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result['hard_count'] == (0 if verdict is False else 1)
    assert result['review_status'] == ('complete' if type(verdict) is bool else 'incomplete')
    assert result['stages'][-1]['finding']['location'] == HARD['location']


@pytest.mark.asyncio
async def test_style_pattern_is_an_observation_not_a_forced_rewrite(critic_io, monkeypatch):
    critic_io(lambda _: {'issues': []})
    monkeypatch.setattr(critic_service, 'scan_anti_ai', AsyncMock(return_value=[{
        'severity': 'hard', 'category': 'anti_ai', 'desc': '出现某个常用词。', 'location': '',
    }]))
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result['hard_count'] == 0 and len(result['observations']) == 1
    assert VariantCandidate(0, DRAFT, result).score == 0


@pytest.mark.asyncio
async def test_split_calls_do_not_use_the_same_session_concurrently(critic_io, monkeypatch):
    import asyncio
    active = 0
    peak = 0
    async def call(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        return NS(text='{"issues": []}', finish_reason='stop')
    monkeypatch.setattr(critic_service, 'run_text_prompt', call)
    await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert peak == 1


@pytest.mark.parametrize('mutate', [
    lambda r: r.update(hard_count=-1),
    lambda r: r.update(hard_count=True),
    lambda r: r.update(soft_count=6),
    lambda r: r.update(stages=[]),
    lambda r: r.update(stages=[{'name': 'critic_hard', 'status': 'complete'}]),
    lambda r: r.update(stages=[{'name': 'critic', 'status': 'not_applicable'}]),
    lambda r: r.update(review_version=True),
])
def test_broken_receipt_cannot_be_ranked(mutate):
    report = review()
    mutate(report)
    assert rank([VariantCandidate(0, DRAFT, report)]) == []


@pytest.mark.parametrize('stages', [
    [{'name': 'critic', 'status': 'not_applicable'}, {'name': 'anti_ai', 'status': 'complete'}],
    [{'name': 'critic_hard', 'status': 'complete'}, {'name': 'critic_soft', 'status': 'not_applicable'}],
    [{'name': 'critic_hard', 'status': 'not_applicable'}, {'name': 'critic_soft', 'status': 'complete'}],
    [{'name': 'critic_hard', 'status': 'not_applicable'}, {'name': 'critic_soft', 'status': 'not_applicable'},
     {'name': 'anti_ai', 'status': 'complete'}],
    [{'name': 'critic', 'status': 'complete'}, {'name': 'critic_hard', 'status': 'not_applicable'}],
])
def test_observation_or_partial_streams_cannot_replace_required_critic(stages):
    from app.services.review_contract import critic_next_action, critic_review_status, make_critic_report

    generated = make_critic_report(DRAFT, issues=[], stages=stages)
    assert generated['review_status'] == 'incomplete'
    claimed_complete = {**generated, 'review_status': 'complete'}
    assert critic_review_status(claimed_complete, DRAFT) == 'incomplete'
    assert critic_next_action(claimed_complete, DRAFT, 0, 1) == 'review_incomplete'
    assert rank([VariantCandidate(0, DRAFT, claimed_complete)]) == []


@pytest.mark.parametrize('names', [('critic',), ('critic_hard', 'critic_soft')])
def test_optional_observation_does_not_invalidate_finished_critic(names):
    from app.services.review_contract import critic_review_status, make_critic_report

    stages = [{'name': name, 'status': 'complete'} for name in names]
    stages.append({'name': 'optional_observation', 'status': 'not_applicable'})
    result = make_critic_report(DRAFT, issues=[], stages=stages)
    assert critic_review_status(result, DRAFT) == 'complete'


@pytest.mark.parametrize('raw', [
    '{"issues": [], "score": 1e999}',
    '{"issues": [{"metadata": {"confidence": -1e999}}]}',
])
def test_review_json_rejects_exponent_overflow_anywhere(raw):
    from app.services.review_contract import parse_review_json

    with pytest.raises(ValueError, match='review_response_invalid_json'):
        parse_review_json(NS(text=raw, finish_reason='stop'))


def test_many_soft_issues_do_not_average_away_one_hard_conflict():
    soft = [{**HARD, 'severity': 'soft'} for _ in range(12)]
    hard_candidate = VariantCandidate(0, DRAFT, review(issues=[HARD]))
    soft_candidate = VariantCandidate(1, DRAFT, review(issues=soft))
    assert rank([hard_candidate, soft_candidate]) == [soft_candidate, hard_candidate]


class FixedChecker(BaseChecker):
    name = 'fixed'
    def __init__(self, result): self.result = result
    async def check(self, *_): return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, {}, CheckResult('wrong'), CheckResult('fixed', score=float('nan')),
                                    CheckResult('fixed', score=float('inf')), CheckResult('fixed', score=-1),
                                    CheckResult('fixed', score=True), CheckResult('fixed', issues=[None])])
async def test_invalid_checker_result_cannot_mark_the_review_complete(bad):
    result = await CheckerManager(checkers=[FixedChecker(bad)]).run_all(DRAFT, ContextPack())
    assert result.to_dict()['review_status'] == 'incomplete'
    assert result.overall_score is None and not result.passed
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.asyncio
async def test_incomplete_check_keeps_other_findings_without_inventing_a_content_error():
    known = CheckResult('fixed')
    known.add_issue('resource', 'high', location='两千元', description='核对约定金额。')
    result = await CheckerManager(checkers=[FixedChecker(known), BrokenChecker()]).run_all(DRAFT, ContextPack())
    assert result.total_issues == 1 and len(result.high_issues) == 1
    assert result.overall_score is None
    assert result.checker_results[0].score == 10  # No score-based compensation.
    assert result.passed is False and result.completed is False


@pytest.mark.asyncio
async def test_unknown_selected_checker_is_not_silently_ignored():
    checker = FixedChecker(CheckResult('fixed'))
    result = await CheckerManager(checkers=[checker]).run_selected(DRAFT, ContextPack(), ['fixed', 'missing'])
    assert not result.completed and not result.passed and result.overall_score is None
    assert len(result.checker_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['consistency', 'ooc'])
@pytest.mark.parametrize('response', ['garbage [] trailing', '{}', '[null]', '[]'])
async def test_internal_checker_parse_failure_stays_unavailable(monkeypatch, name, response):
    from app.services.checkers.ooc_checker import OOCChecker
    from app.services.context_pack import CharacterCard
    router = NS(generate_with_tier_fallback=AsyncMock(return_value=NS(text=response, finish_reason='length' if response == '[]' else 'stop')))
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=router))
    checker = ConsistencyChecker() if name == 'consistency' else OOCChecker()
    context = ContextPack(world_rules=['钱必须实际交付。'], character_cards=[CharacterCard(name='沈青')])
    result = await checker.check(DRAFT, context)
    assert result.completed is False and result.passed is False and result.score is None
    assert result.issues == []


@pytest.mark.asyncio
async def test_ooc_review_reads_the_ending_and_all_prepared_characters(monkeypatch):
    from app.services.checkers.ooc_checker import OOCChecker
    from app.services.context_pack import CharacterCard
    router = NS(generate_with_tier_fallback=AsyncMock(return_value=NS(text='[]', finish_reason='stop')))
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=router))
    text = DRAFT * 250 + '章末，她将来时的信烧了。'
    cards = [CharacterCard(name=f'人物{n}') for n in range(7)]
    result = await OOCChecker().check(text, ContextPack(character_cards=cards, cognition_boundaries='她仍以为信没有寄出，误信尚未消除。'))
    assert result.completed is True
    sent = router.generate_with_tier_fallback.await_args.kwargs['messages'][-1]['content']
    assert text in sent and '人物6' in sent
    assert '误信尚未消除' in sent


@pytest.mark.parametrize('report,limit,expected', [
    ({}, 3, 'needs_review'), (review(status='incomplete'), 3, 'needs_review'),
    (review(issues=[HARD]), 0, 'needs_review'), (review(issues=[HARD]), 1, 'rewrite'),
    (review(issues=[{**HARD, 'severity': 'soft'}]), 3, 'needs_review'), (review(), 0, 'finalize'),
])
def test_graph_and_runner_share_completion_rules(report, limit, expected):
    from app.graphs.generation_graph import route_critic
    assert route_critic({'critic_report': report, 'draft_text': DRAFT, 'max_rewrite_count': limit}) == expected


@pytest.mark.asyncio
async def test_retired_standalone_stub_has_an_explicit_workflow_error():
    from fastapi import HTTPException
    from app.api.quality import bvsr_generate, BVSRRequest
    with pytest.raises(HTTPException) as caught:
        await bvsr_generate(BVSRRequest(prompt='写一章'))
    assert caught.value.status_code == 410
    assert caught.value.detail['generation_endpoint'] == '/api/generation-runs/start'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    '{"issues": [], "issues": []}', '{"issues": [], "score": NaN}',
    '{"issues": [], "parse_error": true}',
    '{"issues": [], "review_status": "incomplete", "reason": "缺少前文"}',
])
async def test_response_cannot_hide_incomplete_review_behind_an_empty_list(critic_io, monkeypatch, raw):
    monkeypatch.setattr(critic_service, 'run_text_prompt', AsyncMock(return_value=NS(text=raw, finish_reason='stop')))
    result = await critic_service.run_critic(draft=DRAFT, project_id='p', chapter_id='c', db=NS())
    assert result['review_status'] == 'incomplete'


@pytest.mark.asyncio
async def test_issue_severity_cannot_disagree_with_a_passed_flag():
    raw = CheckResult('fixed', issues=[{'severity': 'high', 'description': '需要核对。'}])
    result = await CheckerManager(checkers=[FixedChecker(raw)]).run_all(DRAFT, ContextPack())
    assert result.completed and not result.passed


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [True, False])
async def test_quality_api_exposes_completion_and_exact_draft_identity(monkeypatch, failed):
    from app.api import quality
    from app.services.review_contract import draft_digest
    text = DRAFT * 200 + '末尾也必须进入检查。'
    context = ContextPack()
    class InspectingChecker(BaseChecker):
        name = 'consistency'
        async def check(self, chapter_text, _context):
            assert chapter_text == text
            if failed: raise RuntimeError('private route detail')
            return CheckResult('consistency')
    manager = CheckerManager(checkers=[InspectingChecker()])
    monkeypatch.setattr('app.services.checkers.checker_manager.CheckerManager', lambda: manager)
    monkeypatch.setattr('app.services.context_pack.ContextPackBuilder', lambda: NS(build=AsyncMock(return_value=context), close=AsyncMock()))
    db = NS(get=AsyncMock(side_effect=[NS(content_text=text, volume_id='v', chapter_idx=1), NS(project_id='p')]), rollback=AsyncMock())
    result = await quality.check_quality('c', None, db)
    assert result['review_status'] == ('incomplete' if failed else 'complete')
    assert result['passed'] is (not failed)
    assert result['chapter_id'] == 'c' and result['draft_sha256'] == draft_digest(text)
    assert result['checkers'][0]['name'] == 'consistency'
    assert 'private route detail' not in json.dumps(result)


@pytest.mark.asyncio
async def test_explicit_empty_review_text_does_not_review_another_saved_draft():
    from fastapi import HTTPException
    from app.api.quality import check_quality, CheckQualityRequest
    db = NS(get=AsyncMock(return_value=NS(content_text=DRAFT)))
    with pytest.raises(HTTPException) as caught:
        await check_quality('c', CheckQualityRequest(chapter_text=''), db)
    assert caught.value.status_code == 400
