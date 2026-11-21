"""Budget and evidence contracts; synthetic model responses are not literary review."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.chapter_entity_extraction import FIELDS, EntityExtractionError, extract_entity_changes
from app.services.entity_sources import EntitySource, digest
from app.services.relationship_identity import relationship_record_id
from app.services import relationship_scope as scope
from app.services.relationship_scope_delivery import (
    COMPLETE, SCOPE_PENDING, InvalidRetainedPayload, extraction_payload_hash, validate_retained_payload,
)

BODY = '陈川与沈青断绝私人往来。公事照办。'


def sample(count=2, body=BODY):
    pid, cid, prior = map(str, (uuid4(), uuid4(), uuid4()))
    source = EntitySource(cid, pid, 901, body, {}, digest(body))
    payload = {field: [] for field in FIELDS}
    payload['changed_relationships'] = [dict(char1='陈川', char2='沈青', type='绝交', evidence=BODY)]
    candidates = []
    for i in range(count):
        kind = ('朋友', '同事')[i] if i < 2 else f'约定{i}（本次交接前有效）'
        candidates.append(dict(record_id=relationship_record_id(pid, '陈川', '沈青', kind, prior, digest('前章')),
            char1='陈川', char2='沈青', type=kind, source_chapter_id=prior, source_hash=digest('前章'),
            global_idx=900, evidence='前章的完整引文。', scope_pending=None))
    return NS(source=source, payload=payload, candidates=candidates)


def reviews(candidates, quote=BODY):
    return [dict(record_id=row['record_id'], ended=row['type'] == '朋友',
                 evidence=quote if row['type'] == '朋友' else '') for row in candidates]


def plan_for(r):
    return scope.build_scope_plan(r.payload, r.candidates, r.source.content, system_chars=len(scope.SYSTEM_PROMPT))


def test_all_records_of_one_pair_are_batched_without_losing_full_wording():
    r = sample(37)
    plan = plan_for(r)
    assert [len(batch['candidates']) for batch in plan['batches']] == [16, 16, 5]
    sent = [row for batch in plan['batches'] for row in batch['candidates']]
    assert sorted(sent, key=lambda row: row['record_id']) == sorted(r.candidates, key=lambda row: row['record_id'])
    assert scope.validate_scope_plan(r.payload, plan, r.source) == plan
    r.candidates[0]['type'] = '后来改过的身份'
    assert all(row['type'] != '后来改过的身份' for row in sent)


def test_large_relevant_pair_keeps_all_5000_candidates_with_complete_chapter_per_batch():
    from time import perf_counter
    r = sample(5000, '完整正文。' * 1800 + BODY)
    started = perf_counter()
    plan = plan_for(r)
    scope.validate_scope_plan(r.payload, plan, r.source)
    elapsed = perf_counter() - started
    candidates = [row['record_id'] for batch in plan['batches'] for row in batch['candidates']]
    assert len(candidates) == len(set(candidates)) == 5000
    assert set(candidates) == {row['record_id'] for row in r.candidates}
    assert len(plan['batches']) == 313
    lengths = []
    for batch in plan['batches']:
        prompt = scope.scope_user_prompt(r.payload['changed_relationships'][0], batch['candidates'], r.source.content)
        assert r.source.content in prompt
        length = len(prompt) + len(scope.SYSTEM_PROMPT) + scope.REPAIR_RESERVE_CHARS
        assert length <= scope.MAX_INPUT_CHARS
        lengths.append(length)
    print(f'REL_SCOPE_5000_BATCHES={len(lengths)} MAX_CHARS_WITH_RESERVE={max(lengths)} PLAN_SECONDS={elapsed:.6f}')


def test_character_budget_splits_before_dropping_any_quote_or_chapter_tail():
    r = sample(2, '正文。' * 5000 + BODY)
    for row in r.candidates:
        row['evidence'] = '旧章完整引文。' * 2500 + '不能丢掉的限定句。'
    plan = plan_for(r)
    assert len(plan['batches']) == 2
    for batch in plan['batches']:
        prompt = scope.scope_user_prompt(r.payload['changed_relationships'][0], batch['candidates'], r.source.content)
        assert r.source.content in prompt and '不能丢掉的限定句。' in prompt
        assert len(prompt) + len(scope.SYSTEM_PROMPT) + scope.REPAIR_RESERVE_CHARS <= scope.MAX_INPUT_CHARS
    assert {row['record_id'] for batch in plan['batches'] for row in batch['candidates']} == {
        row['record_id'] for row in r.candidates}


def test_budget_accounts_for_the_actual_registered_system_at_the_boundary():
    r = sample(1)
    available = scope.MAX_INPUT_CHARS - scope.REPAIR_RESERVE_CHARS - len(
        scope.scope_user_prompt(r.payload['changed_relationships'][0], r.candidates, r.source.content))
    assert len(scope.build_scope_plan(r.payload, r.candidates, r.source.content, system_chars=available)['batches']) == 1
    with pytest.raises(scope.RelationshipScopeError, match='input_budget_exceeded'):
        scope.build_scope_plan(r.payload, r.candidates, r.source.content, system_chars=available + 1)


def test_single_oversized_record_or_full_chapter_fails_instead_of_clipping():
    r = sample(2)
    r.candidates[-1]['type'] = '完整关系。' * scope.MAX_INPUT_CHARS
    with pytest.raises(scope.RelationshipScopeError, match='input_budget_exceeded'): plan_for(r)
    r = sample(1, BODY + '完整正文。' * scope.MAX_INPUT_CHARS)
    with pytest.raises(scope.RelationshipScopeError, match='input_budget_exceeded'): plan_for(r)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'unknown', 'int_bool', 'string_bool', 'null_bool',
                                  'bad_quote', 'false_bad_quote', 'empty_true_quote', 'extra_field', 'not_array'])
def test_each_candidate_requires_exactly_one_strict_review(case):
    r = sample()
    data = {'reviews': reviews(r.candidates)}
    if case == 'missing': data['reviews'].pop()
    if case == 'duplicate': data['reviews'][1] = deepcopy(data['reviews'][0])
    if case == 'unknown': data['reviews'][0]['record_id'] = 'rel_' + '0' * 64
    if case == 'int_bool': data['reviews'][0]['ended'] = 1
    if case == 'string_bool': data['reviews'][0]['ended'] = 'true'
    if case == 'null_bool': data['reviews'][0]['ended'] = None
    if case == 'bad_quote': data['reviews'][0]['evidence'] = '正文没有这句结束关系的话。'
    if case == 'false_bad_quote': data['reviews'][1]['evidence'] = '正文没有这句。'
    if case == 'empty_true_quote': data['reviews'][0]['evidence'] = ' '
    if case == 'extra_field': data['reviews'][0]['reason'] = '猜测'
    if case == 'not_array': data['reviews'] = {}
    with pytest.raises(scope.RelationshipScopeError):
        scope.validate_scope_reviews(data, r.candidates, r.source.content)


def test_false_reviews_are_retained_and_only_true_ids_end_relationships():
    r = sample()
    plan = plan_for(r)
    plan['batches'][0]['reviews'] = reviews(plan['batches'][0]['candidates'])
    result = scope.finish_scope_payload(r.payload, plan, r.source)
    assert result['changed_relationships'][0]['replaces'] == [r.candidates[0]['record_id']]
    assert 'replaces' not in r.payload['changed_relationships'][0]
    assert any(row['ended'] is False for row in plan['batches'][0]['reviews'])


def test_end_evidence_and_change_evidence_remain_one_real_contiguous_source_span():
    tail = '沈青亲手烧了两人私下往来的最后一封信。'
    r = sample(1, BODY + '中间仍有完整正文。' * 900 + tail)
    plan = plan_for(r)
    plan['batches'][0]['reviews'] = reviews(r.candidates, tail)
    result = scope.finish_scope_payload(r.payload, plan, r.source)
    assert result['changed_relationships'][0]['evidence'] == r.source.content


def test_multiple_changes_can_review_the_same_original_record_independently():
    r = sample(1)
    r.payload['changed_relationships'].append(dict(char1='陈川', char2='沈青', type='停止私下往来', evidence=BODY))
    plan = plan_for(r)
    for batch in plan['batches']: batch['reviews'] = reviews(batch['candidates'])
    result = scope.finish_scope_payload(r.payload, plan, r.source)
    assert len(result['changed_relationships']) == 2
    assert all(row['replaces'] == [r.candidates[0]['record_id']] for row in result['changed_relationships'])


@pytest.mark.parametrize('case', ['wrong_base', 'wrong_version', 'wrong_event', 'wrong_pair', 'future', 'same_chapter',
                                  'bad_id', 'missing_source', 'bad_source', 'missing_wording', 'duplicate_batch', 'unfinished'])
def test_invalid_or_incomplete_plan_cannot_become_a_complete_payload(case):
    r = sample(1)
    plan = plan_for(r)
    batch = plan['batches'][0]
    batch['reviews'] = reviews(batch['candidates'])
    if case == 'wrong_base': plan['base_hash'] = digest('other')
    if case == 'wrong_version': plan['version'] = True
    if case == 'wrong_event': batch['event_index'] = 4
    if case == 'wrong_pair': batch['candidates'][0]['char1'] = '沈青'
    if case == 'future': batch['candidates'][0]['global_idx'] = 902
    if case == 'same_chapter': batch['candidates'][0]['global_idx'] = 901
    if case == 'bad_id': batch['candidates'][0]['record_id'] = 'internal-neo4j-id'
    if case == 'missing_source': batch['candidates'][0].pop('source_hash')
    if case == 'bad_source': batch['candidates'][0]['source_chapter_id'] = 'old chapter'
    if case == 'missing_wording': batch['candidates'][0]['type'] = ''
    if case == 'duplicate_batch': plan['batches'].append(deepcopy(batch))
    if case == 'unfinished': batch['reviews'] = None
    with pytest.raises(scope.RelationshipScopeError): scope.finish_scope_payload(r.payload, plan, r.source)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['{}', '{"reviews": [], "reviews": []}', '{"reviews": NaN}', '```json\n{}'])
async def test_one_format_repair_keeps_all_candidates_and_the_complete_chapter(monkeypatch, bad):
    r = sample(2, '完整正文。' * 1000 + BODY)
    batch = plan_for(r)['batches'][0]
    good = {'reviews': reviews(batch['candidates'])}
    model = AsyncMock(side_effect=[NS(text=bad, finish_reason='stop'), NS(text=json.dumps(good), finish_reason='stop')])
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    assert await scope.review_scope_batch(r.source, r.payload['changed_relationships'][0], batch, object()) == good['reviews']
    assert model.await_count == 2
    for call in model.await_args_list:
        assert r.source.content in call.kwargs['user_content']
        assert all(row['record_id'] in call.kwargs['user_content'] for row in r.candidates)
        assert call.kwargs['input_char_limit'] == scope.MAX_INPUT_CHARS
        assert call.kwargs['chapter_id'] == r.source.chapter_id
    assert len(model.await_args_list[1].kwargs['user_content']) - len(model.await_args_list[0].kwargs['user_content']) < scope.REPAIR_RESERVE_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls', 'MAX_TOKENS'])
async def test_parseable_truncated_or_filtered_review_is_rejected_without_format_retry(monkeypatch, finish):
    r = sample()
    batch = plan_for(r)['batches'][0]
    model = AsyncMock(return_value=NS(text=json.dumps({'reviews': reviews(batch['candidates'])}), finish_reason=finish))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    with pytest.raises(ValueError):
        await scope.review_scope_batch(r.source, r.payload['changed_relationships'][0], batch, object())
    model.assert_awaited_once()


@pytest.mark.parametrize('change', ['payload', 'plan', 'source', 'stage', 'missing_hash'])
def test_partial_receipt_binds_every_cached_input_and_completed_review(change):
    r = sample()
    plan = plan_for(r)
    plan['batches'][0]['reviews'] = reviews(plan['batches'][0]['candidates'])
    receipt = extraction_payload_hash(r.source.source_hash, r.payload, SCOPE_PENDING, plan)
    assert validate_retained_payload(r.source, r.payload, receipt, SCOPE_PENDING, plan)
    stage = SCOPE_PENDING
    if change == 'payload': r.payload['changed_relationships'][0]['type'] = '另一个变化'
    if change == 'plan': plan['batches'][0]['reviews'] = None
    if change == 'source': r.source = replace(r.source, source_hash=digest('new source'))
    if change == 'stage': stage, plan = COMPLETE, None
    if change == 'missing_hash': receipt = None
    with pytest.raises(InvalidRetainedPayload): validate_retained_payload(r.source, r.payload, receipt, stage, plan)


def test_legacy_complete_payload_without_scope_is_reusable_but_partial_is_not():
    r = sample()
    legacy_hash = digest([r.source.source_hash, r.payload])
    assert extraction_payload_hash(r.source.source_hash, r.payload) == legacy_hash
    assert validate_retained_payload(r.source, r.payload, legacy_hash, COMPLETE, None)
    with pytest.raises(InvalidRetainedPayload):
        validate_retained_payload(r.source, r.payload, legacy_hash, SCOPE_PENDING, None)


@pytest.mark.asyncio
async def test_initial_pass_uses_deferred_schema_and_rejects_premature_end_ids(monkeypatch):
    r = sample()
    premature = deepcopy(r.payload)
    premature['changed_relationships'][0]['replaces'] = [r.candidates[0]['record_id']]
    model = AsyncMock(side_effect=[NS(text=json.dumps(premature)), NS(text=json.dumps(r.payload))])
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    assert await extract_entity_changes(project_id=r.source.project_id, chapter_text=BODY, db=object(),
                                        defer_relationship_scope=True) == r.payload
    assert model.await_count == 2
    assert '同方向、同一对人物的前文记录' not in model.await_args_list[0].kwargs['user_content']
    with pytest.raises(EntityExtractionError):
        await extract_entity_changes(project_id=r.source.project_id, chapter_text=BODY, db=object(),
            relationship_candidates=r.candidates, defer_relationship_scope=True)
