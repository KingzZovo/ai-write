"""The model must identify exact prior records, backed by the full source."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.chapter_entity_extraction import (
    EntityExtractionError, FIELDS, extract_entity_changes, validate_entity_changes,
)
from app.services.entity_context import verified_relationship_candidates
from tests.test_character_state_context import state_db, add_state
from tests.test_relationship_context import Graph, edge, reference


def changes(body, refs=()):
    data = {key: [] for key in FIELDS}
    data['changed_relationships'] = [dict(char1='陈川', char2='沈青', type='绝交', evidence=body,
                                          replaces=list(refs))]
    return data


@pytest.mark.asyncio
async def test_live_user_prompt_includes_scope_rules_full_source_and_verified_old_quotes(state_db, monkeypatch):
    r = state_db
    first = await add_state(r, 900, {}, body='陈川和沈青相交多年，是同事，也是朋友。')
    candidates = await verified_relationship_candidates(r.db, Graph([
        edge(first, kind, evidence=first.content) for kind in ('同事', '朋友')]), r.pid, 901)
    body = '完整正文。' * 1800 + '陈川说：“我们的交情从此断了，公事照办。”'
    payload = changes(body, [reference(first)])
    model = AsyncMock(return_value=NS(text=json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    assert await extract_entity_changes(project_id=r.pid, chapter_text=body, db=object(),
                                         relationship_candidates=candidates) == payload
    call = model.call_args.kwargs
    text = call['user_content']
    assert call['task_type'] == 'chapter_entities' and call['project_id'] == r.pid
    assert body in text and first.content in text
    assert all(candidate['record_id'] in text for candidate in candidates)
    assert '反目不等于解除亲属或同事关系' in text
    assert '不拆掉限定语或复合身份' in text and 'replaces' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'unknown', 'cross_pair', 'reversed', 'duplicate_id', 'bad_shape', 'duplicate_event'])
async def test_invalid_scope_gets_one_repair_and_never_becomes_silent_success(state_db, monkeypatch, case):
    r = state_db
    first = await add_state(r, 900, {})
    candidates = await verified_relationship_candidates(r.db, Graph([edge(first)]), r.pid, 901)
    body = '陈川与沈青绝交了。'
    bad = changes(body, [reference(first)])
    change = bad['changed_relationships'][0]
    if case == 'missing': change.pop('replaces')
    elif case == 'unknown': change['replaces'] = ['rel_' + 'f' * 64]
    elif case == 'cross_pair': change['char2'] = '阿玉'
    elif case == 'reversed': change['char1'], change['char2'] = change['char2'], change['char1']
    elif case == 'duplicate_id': change['replaces'] *= 2
    elif case == 'bad_shape': change['replaces'] = reference(first)
    else: bad['new_relationships'] = [{key: value for key, value in change.items() if key != 'replaces'}]
    model = AsyncMock(return_value=NS(text=json.dumps(bad, ensure_ascii=False)))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    with pytest.raises(EntityExtractionError):
        await extract_entity_changes(project_id=r.pid, chapter_text=body, db=object(), relationship_candidates=candidates)
    assert model.await_count == 2
    assert '【校验问题】' in model.call_args.kwargs['user_content']
    assert all(body in call.kwargs['user_content'] for call in model.call_args_list)


@pytest.mark.asyncio
async def test_repair_keeps_valid_new_facts_and_uses_the_original_reference_catalog(state_db, monkeypatch):
    r = state_db
    first = await add_state(r, 900, {})
    candidates = await verified_relationship_candidates(r.db, Graph([edge(first)]), r.pid, 901)
    body = '陈川与沈青绝交了。'
    fixed = changes(body, [reference(first)])
    fixed['state_changes'] = [dict(name='陈川', changes={'所在': '码头'}, evidence=body)]
    invalid = changes(body)
    invalid['changed_relationships'][0].pop('replaces')
    model = AsyncMock(side_effect=[NS(text=json.dumps(invalid)), NS(text=json.dumps(fixed))])
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    actual = await extract_entity_changes(project_id=r.pid, chapter_text=body, db=object(), relationship_candidates=candidates)
    assert actual == fixed
    for call in model.call_args_list:
        assert reference(first) in call.kwargs['user_content']


def test_retained_payload_accepts_missing_scope_without_mutating_it_or_inventing_targets():
    body = '陈川与沈青反目了。'
    data = changes(body)
    data['changed_relationships'][0].pop('replaces')
    original = json.dumps(data, ensure_ascii=False)
    assert validate_entity_changes(data, body) == data
    assert json.dumps(data, ensure_ascii=False) == original
    with pytest.raises(EntityExtractionError):
        validate_entity_changes(data, body, require_replaces=True, relationship_candidates=[])


@pytest.mark.parametrize('refs', [None, 'old', ['not-an-id'], [False], ['rel_'+'a'*64]*2])
def test_malformed_persisted_scope_is_rejected_before_any_projection(refs):
    body = '陈川与沈青绝交了。'
    data = changes(body)
    data['changed_relationships'][0]['replaces'] = refs
    with pytest.raises(EntityExtractionError):
        validate_entity_changes(data, body)


@pytest.mark.asyncio
async def test_no_catalog_allows_explicit_empty_scope_but_rejects_invented_targets(monkeypatch):
    body = '陈川与沈青争执了一番。'
    data = changes(body)
    model = AsyncMock(return_value=NS(text=json.dumps(data)))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    assert await extract_entity_changes(project_id='p', chapter_text=body, db=object()) == data
    data['changed_relationships'][0]['replaces'] = ['rel_' + 'a' * 64]
    model.return_value = NS(text=json.dumps(data))
    with pytest.raises(EntityExtractionError):
        await extract_entity_changes(project_id='p', chapter_text=body, db=object())


@pytest.mark.asyncio
@pytest.mark.parametrize('finish_reason', ['length', 'max_tokens', 'content_filter', 'tool_calls'])
async def test_explicitly_incomplete_model_output_is_not_accepted_even_if_json_parses(monkeypatch, finish_reason):
    body = '陈川与沈青争执了一番。'
    model = AsyncMock(return_value=NS(text=json.dumps(changes(body)), finish_reason=finish_reason))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    with pytest.raises(ValueError, match='provider_incomplete_response'):
        await extract_entity_changes(project_id='p', chapter_text=body, db=object())
    model.assert_awaited_once()
