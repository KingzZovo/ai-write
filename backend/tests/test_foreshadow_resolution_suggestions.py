import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.services import foreshadow_manager as module


def setup(monkeypatch, response):
    row = NS(id=uuid4(), description='信是谁寄的', resolve_conditions_json=['寄信人身份被证实'],
             status='ready', resolved_chapter=None, narrative_proximity=0.95, type='mystery',
             planted_chapter=1, resolution_blueprint_json={}, source='lifecycle')
    mapping = lambda: {key: getattr(row, key) for key in ('id', *module.REVIEW_SOURCE_FIELDS)}
    db = NS(execute=AsyncMock(return_value=NS(mappings=lambda: NS(
        all=lambda: [mapping()], one_or_none=mapping))), flush=AsyncMock())
    router = NS(generate_with_tier_fallback=AsyncMock(return_value=NS(text=response)))
    monkeypatch.setattr(module, 'get_model_router', lambda: router)
    return module.ForeshadowManager(db), row, db, router


@pytest.mark.asyncio
async def test_exact_evidence_after_old_cutoff_returns_suggestion_without_mutating_facts(monkeypatch):
    evidence = '陈伯把信封推过来：“信是我寄的，邮局的存根在这里。”'
    prose = '雨一直下。' * 700 + evidence
    manager, row, db, router = setup(monkeypatch, json.dumps({
        'resolved': True, 'evidence': evidence, 'reason': '陈伯承认寄信，并拿出存根；仍需核对原始悬念。',
    }))
    results = await manager.check_resolution(str(uuid4()), prose, chapter_idx=31)
    assert db.execute.call_args.args[0].get_execution_options()['autoflush'] is False
    assert len(results) == 1
    candidate = results[0]
    assert candidate.foreshadow_id == str(row.id) and candidate.chapter_idx == 31
    assert prose[candidate.evidence_start:candidate.evidence_end] == evidence
    assert candidate.evidence_start > 3000
    assert candidate.foreshadow_description == row.description
    assert candidate.foreshadow_sha256 == module.foreshadow_source_hash(vars(row))
    assert candidate.source_sha256 == hashlib.sha256(prose.encode('utf-8')).hexdigest()
    assert prose in router.generate_with_tier_fallback.call_args.kwargs['messages'][1]['content']
    assert row.status == 'ready' and row.resolved_chapter is None and row.narrative_proximity == 0.95
    db.flush.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    '{"resolved":"false"}', '{"resolved":"true"}', '{"resolved":1}',
    'true', '[{"resolved":true}]', 'not false, true',
    '{"resolved":true}',
    '{"resolved":true,"evidence":"不存在的原文","reason":"猜测"}',
    '{"resolved":true,"evidence":" ","reason":"猜测"}',
    '{"resolved":true,"evidence":"存根","reason":""}',
])
async def test_malformed_boolean_or_missing_evidence_never_confirms_payoff(monkeypatch, response):
    manager, row, db, router = setup(monkeypatch, response)
    with pytest.raises(module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '他在找存根。', chapter_idx=31)
    assert row.status == 'ready' and row.resolved_chapter is None
    db.flush.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('chapter_idx', [None, True, -1, '2'])
async def test_unknown_chronology_does_not_call_model(monkeypatch, chapter_idx):
    manager, row, db, router = setup(monkeypatch, '{}')
    with pytest.raises(ValueError, match='全书章号'):
        await manager.check_resolution(str(uuid4()), '正文', chapter_idx=chapter_idx)
    db.execute.assert_not_called()
    router.generate_with_tier_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_source_is_not_silently_truncated(monkeypatch):
    manager, row, db, router = setup(monkeypatch, '{}')
    with pytest.raises(ValueError, match='预算'):
        await manager.check_resolution(str(uuid4()), '字' * 24001, chapter_idx=31)
    db.execute.assert_not_called()
    router.generate_with_tier_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_materials_are_counted_in_full_prompt_budget(monkeypatch):
    manager, row, db, router = setup(monkeypatch, '{}')
    row.description = '字' * 24000
    with pytest.raises(ValueError, match='预算'):
        await manager.check_resolution(str(uuid4()), '正文', chapter_idx=31)
    router.generate_with_tier_fallback.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_truncation_is_not_a_completed_review(monkeypatch):
    manager, row, db, router = setup(monkeypatch, json.dumps({
        'resolved': True, 'evidence': '存根', 'reason': '模型认为证据完整',
    }))
    router.generate_with_tier_fallback.return_value.finish_reason = 'length'
    with pytest.raises(module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '他在找存根。', chapter_idx=31)
    assert row.status == 'ready'
    db.flush.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('field, changed', [
    ('description', '信里的暗号是什么意思'), ('resolve_conditions_json', ['暗号得到解读']),
    ('resolution_blueprint_json', {'reveal': '口供有假'}), ('status', 'resolved'),
    ('resolved_chapter', 30), ('planted_chapter', 8), ('source', 'neo4j'), ('type', 'plot'),
])
async def test_changed_foreshadow_contract_discards_candidate(monkeypatch, field, changed):
    response = json.dumps({'resolved': True, 'evidence': '存根', 'reason': '出现了寄信存根'})
    manager, row, db, router = setup(monkeypatch, response)
    async def generate(**kwargs):
        setattr(row, field, changed)
        return NS(text=response)
    router.generate_with_tier_fallback.side_effect = generate
    with pytest.raises(module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '陈伯拿出了存根。', chapter_idx=31)
    assert db.execute.await_count == 2
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_deleted_foreshadow_discards_candidate(monkeypatch):
    response = json.dumps({'resolved': True, 'evidence': '存根', 'reason': '出现了寄信存根'})
    manager, row, db, router = setup(monkeypatch, response)
    initial = db.execute.return_value
    db.execute.side_effect = [initial, NS(mappings=lambda: NS(all=lambda: []))]
    with pytest.raises(module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '陈伯拿出了存根。', chapter_idx=31)
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_score_refresh_does_not_change_the_reviewed_contract(monkeypatch):
    response = json.dumps({'resolved': True, 'evidence': '存根', 'reason': '出现了寄信存根'})
    manager, row, db, router = setup(monkeypatch, response)
    async def generate(**kwargs):
        row.narrative_proximity = 0.99
        return NS(text=response)
    router.generate_with_tier_fallback.side_effect = generate
    assert len(await manager.check_resolution(str(uuid4()), '陈伯拿出了存根。', chapter_idx=31)) == 1
    from sqlalchemy.dialects import postgresql
    compiled = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert 'foreshadows.project_id =' in str(compiled) and 'foreshadows.id IN' in str(compiled)
    assert any(isinstance(value, list) and row.id in value for value in compiled.params.values())


@pytest.mark.asyncio
async def test_earlier_candidate_changed_during_later_inference_is_discarded(monkeypatch):
    monkeypatch.setattr(module, 'RESOLUTION_REVIEW_BATCH_SIZE', 1)
    response = json.dumps({'resolved': True, 'evidence': '存根', 'reason': '发现证据'})
    manager, row, db, router = setup(monkeypatch, response)
    other = NS(**(vars(row) | {'id': uuid4(), 'description': '存根的编号'}))
    def mappings():
        return [{key: getattr(item, key) for key in ('id', *module.REVIEW_SOURCE_FIELDS)} for item in (row, other)]
    db.execute.return_value = NS(mappings=lambda: NS(all=mappings))
    calls = 0
    async def generate(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            row.description = '作者已经改掉了第一个悬念'
        return NS(text=response)
    router.generate_with_tier_fallback.side_effect = generate
    with pytest.raises(module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '存根在桌上。', chapter_idx=31)
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_valid_negative_verdict_is_successfully_reviewed(monkeypatch):
    manager, row, db, router = setup(monkeypatch, '{"resolved":false}')
    assert await manager.check_resolution(str(uuid4()), '正文', chapter_idx=31) == []
