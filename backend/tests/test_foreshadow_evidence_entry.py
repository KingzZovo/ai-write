import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from app.models.project import Foreshadow, ForeshadowReview
from app.services import foreshadow_manager as manager_module
from app.services.foreshadow_review_actions import accept_review
from app.services.foreshadow_review_reader import list_resolution_reviews
from app.services.foreshadow_review_store import persist_resolution_review
from tests.test_foreshadow_review_store import database
from tests.test_foreshadow_resolution_suggestions import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['planted', 'ripening', 'ready', 'planned', 'pending', 'resolved'])
async def test_actual_source_query_accepts_planted_prose_without_score_and_excludes_plans(database, monkeypatch, status):
    session, db, review, _ = database
    session.execute(update(Foreshadow).values(status=status, narrative_proximity=0)); session.commit()
    router = NS(generate_with_tier_fallback=AsyncMock(return_value=NS(text=json.dumps({
        'resolved': True, 'evidence': '寄信的存根', 'reason': '存根与寄信人有关，需作者核对是否回答原悬念',
    }))))
    monkeypatch.setattr(manager_module, 'get_model_router', lambda: router)
    manager = manager_module.ForeshadowManager(db)
    suggestions = await manager.check_resolution(review.project_id, '陈伯把寄信的存根放在桌上。', chapter_idx=913)
    if status in ('planned', 'pending', 'resolved'):
        assert suggestions == []; router.generate_with_tier_fallback.assert_not_called()
        return
    assert len(suggestions) == 1
    assert session.execute(select(Foreshadow.status, Foreshadow.narrative_proximity)).one() == (status, 0)
    session.commit()
    with session.begin(): await persist_resolution_review(db, replace(review, suggestions=tuple(suggestions)))
    rid = session.execute(select(ForeshadowReview.id)).scalar_one(); session.commit()
    with session.begin(): assert await accept_review(db, UUID(review.project_id), rid, actor='writer') == 'accepted'
    assert (await list_resolution_reviews(db, UUID(review.project_id), 'accepted', 20, 0))['reviews'][0]['source_state'] == 'accepted_current'


@pytest.mark.asyncio
async def test_prompts_include_source_checked_clues_without_score_urgency(database, monkeypatch):
    from app.services.context_pack import CFPGTriplet
    session, db, review, _ = database
    session.execute(update(Foreshadow).values(status='planted', narrative_proximity=0.99)); session.commit()
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[{
        'description': '寄信人', 'status': 'planted', 'source_label': '第1卷第1章',
        'source_evidence': '寄信的存根', 'hint': '核对身份', 'narrative_proximity': 0.99,
    }]))
    prompt = await manager_module.ForeshadowManager(db).get_prompts_for_generation(review.project_id, 913)
    assert '寄信人' in prompt and '不必让每条线索在本章出现' in prompt
    assert all(word not in prompt for word in ('99%', '已成熟', '应在本章', '由AI自行安排'))
    low = CFPGTriplet('既有事实', '寄信人', '身份回应', proximity=0).to_prompt()
    high = CFPGTriplet('既有事实', '寄信人', '身份回应', proximity=1).to_prompt()
    assert low == high and '核对正文' in high


def batches(monkeypatch, count):
    manager, first, db, router = setup(monkeypatch, '{}')
    rows = [NS(**(vars(first) | {'id': uuid4(), 'status': 'planted', 'description': f'第{i}条线索'})) for i in range(count)]
    db.execute.return_value = NS(mappings=lambda: NS(all=lambda: [
        {key: getattr(row, key) for key in ('id', *manager_module.REVIEW_SOURCE_FIELDS)} for row in rows]))
    return manager, rows, db, router


@pytest.mark.asyncio
async def test_batches_cover_every_clue_once_with_full_prose_and_explicit_negative_verdicts(monkeypatch):
    manager, rows, db, router = batches(monkeypatch, 19)
    seen = []
    prose = '存根在桌上。' * 500
    async def generate(**kwargs):
        user = kwargs['messages'][1]['content']
        assert user.endswith(prose)
        assert sum(len(m['content']) for m in kwargs['messages']) <= manager_module.RESOLUTION_REVIEW_MAX_INPUT_CHARS
        items = json.loads(user.split('伏笔资料：', 1)[1].split('\n\n待核查完整正文：', 1)[0])
        seen.extend(item['foreshadow_id'] for item in items)
        return NS(text=json.dumps({'reviews': [dict(foreshadow_id=item['foreshadow_id'], resolved=False) for item in items]}))
    router.generate_with_tier_fallback.side_effect = generate
    assert await manager.check_resolution(str(uuid4()), prose, chapter_idx=31) == []
    assert router.generate_with_tier_fallback.await_count == 3
    assert set(seen) == {str(row.id) for row in rows} and len(seen) == 19
    assert db.execute.await_count == 2  # Revalidate even the negative verdicts.


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['missing', 'duplicate', 'foreign', 'not_bool', 'bad_quote'])
async def test_incomplete_or_invalid_batch_cannot_be_reported_as_completed(monkeypatch, error):
    manager, rows, db, router = batches(monkeypatch, 2)
    answers = [dict(foreshadow_id=str(row.id), resolved=False) for row in rows]
    if error == 'missing': answers.pop()
    if error == 'duplicate': answers[1] = answers[0]
    if error == 'foreign': answers[1]['foreshadow_id'] = str(uuid4())
    if error == 'not_bool': answers[1]['resolved'] = 'false'
    if error == 'bad_quote': answers[1].update(resolved=True, evidence='不存在的原句', reason='解释')
    router.generate_with_tier_fallback.return_value = NS(text=json.dumps({'reviews': answers}))
    with pytest.raises(manager_module.ResolutionReviewIncomplete):
        await manager.check_resolution(str(uuid4()), '存根在桌上。', chapter_idx=31)
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_material_budget_splits_before_inference_without_truncation(monkeypatch):
    manager, rows, db, router = batches(monkeypatch, 2)
    rows[0].description = '甲' * 13000; rows[1].description = '乙' * 13000
    router.generate_with_tier_fallback.return_value = NS(text='{"resolved":false}')
    assert await manager.check_resolution(str(uuid4()), '存根在桌上。', chapter_idx=31) == []
    assert router.generate_with_tier_fallback.await_count == 2
    assert rows[0].description in router.generate_with_tier_fallback.await_args_list[0].kwargs['messages'][1]['content']
    assert rows[1].description in router.generate_with_tier_fallback.await_args_list[1].kwargs['messages'][1]['content']


@pytest.mark.asyncio
async def test_late_oversized_candidate_aborts_before_any_model_call(monkeypatch):
    manager, rows, db, router = batches(monkeypatch, 2)
    rows[1].description = '甲' * 24000
    with pytest.raises(ValueError, match='预算'):
        await manager.check_resolution(str(uuid4()), '存根在桌上。', chapter_idx=31)
    router.generate_with_tier_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_negative_result_is_invalidated_when_the_clue_changed_during_review(monkeypatch):
    manager, rows, db, router = batches(monkeypatch, 1)
    async def generate(**kwargs):
        rows[0].description = '作者修改了原问题'
        return NS(text='{"resolved":false}')
    router.generate_with_tier_fallback.side_effect = generate
    with pytest.raises(manager_module.ResolutionReviewIncomplete, match='source_changed'):
        await manager.check_resolution(str(uuid4()), '存根在桌上。', chapter_idx=31)
