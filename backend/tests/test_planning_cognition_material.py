"""Retained manifests and older requests must never fabricate cognition history."""
from copy import deepcopy
import asyncio
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.project import Chapter
from app.services.arc_planning_contract import author_material, previous_material, book_material, source_token, validate_record
from app.services.arc_planning_journal import new_journal
from app.services.arc_planning_sources import read_planning_source
from app.services.entity_sources import digest
from app.services.planning_cognition_material import PlanningCognitionMaterialError, validate_cognition_source
from tests.support.arc_planning_runtime import planning_book, path, request_body, run, snapshot, submit
from tests.test_arc_cognition_material import cognition_book, cognition_material, seed_cognition


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['project', 'boundary', 'empty_coverage', 'observation_position',
                                    'mode', 'reader_belief', 'blank_quote', 'snapshot_hash'])
async def test_retained_material_rejects_inconsistent_sources_even_with_a_rehashed_manifest(cognition_book, damage):
    r = cognition_book; await cognition_material(r)
    async with r.sessions() as db: original = await read_planning_source(db, r.pid)
    source = deepcopy(original['cognition_context'])
    item = source['ledger']['林禾']['history'][0]
    if damage == 'project': source['project_id'] = str(uuid4())
    elif damage == 'boundary':
        source['before_global_idx'] = source['snapshot']['boundary'] = 3
    elif damage == 'empty_coverage':
        for key in ('positions', 'sources', 'decisions', 'proofs'): source['snapshot'][key].clear()
        source['ledger'].clear()
    elif damage == 'observation_position': item['chapter_idx'] = 2
    elif damage == 'mode': item['source_mode'] = 'legacy_unverified'
    elif damage == 'reader_belief': source['ledger']['__reader__']['history'][0]['kind'] = 'believes'
    elif damage == 'blank_quote': item['evidence'] = ' \n'
    source['snapshot_hash'] = digest(source['snapshot']) if damage != 'snapshot_hash' else 'f' * 64
    with pytest.raises(PlanningCognitionMaterialError):
        validate_cognition_source(source, r.pid, original['character_context'])


@pytest.mark.asyncio
async def test_version_four_request_retains_its_actual_inputs_and_cannot_invent_cognition_on_resume(cognition_book):
    r = cognition_book; await cognition_material(r)
    body = await request_body(r, target=4); await submit(r, body)
    with Session(r.engine) as db:
        row = db.get(Request, UUID(body['request_id']))
        source = deepcopy(row.source_json); source['version'] = 4; source.pop('cognition_context')
        intent = deepcopy(row.request_json); intent['expected_source'] = source_token(r.pid, source)
        journal = new_journal(author_material(intent), previous_material(source), book_material(source))
        row.source_json, row.source_hash = source, digest(source)
        row.request_json, row.request_hash = intent, digest(intent)
        row.journal_json, row.journal_hash = journal, digest(journal)
        validate_record(row); db.commit()
    before = snapshot(r, body['request_id'])
    async with r.client() as client: detail = (await client.get(path(r, f'/{body["request_id"]}/detail'))).json()
    assert detail['integrity_valid'] and detail['cognition_material_text'] is None
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['error_code'] == 'arc_planning_source_changed' and not r.calls
    assert after['source_json'] == before['source_json'] and after['journal_json'] == before['journal_json']
    assert after['execution_attempts'] == before['execution_attempts'] == 0


@pytest.mark.asyncio
async def test_required_complete_quote_over_budget_stops_before_embeddings_or_model_calls(cognition_book, monkeypatch):
    r = cognition_book
    prose = '林禾读完借据，才知道欠款还剩一千。' * 1000
    with r.engine.begin() as db:
        db.execute(update(Chapter).where(Chapter.id == r.cid).values(content_text=prose, status='completed'))
    await seed_cognition(r, r.cid, [dict(character='林禾', learns='欠款还剩一千', evidence=prose)])
    loader = AsyncMock(side_effect=AssertionError('must not spend on embeddings'))
    monkeypatch.setattr('app.services.planning_cognition_retrieval.resolve_prompt_selection', loader)
    body = await request_body(r, target=4, core_setup='林禾据借据核对欠款，不临时增加收入。')
    await submit(r, body); await run(r, body['request_id'])
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'needs_review' and state['error_code'] == 'arc_planning_cognition_budget_exceeded'
    assert not r.calls and state['source_json']['cognition_context']['ledger']['林禾']['history'][0]['evidence'] == prose
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_corrupt_selection_stays_reviewable_and_cannot_resume(cognition_book):
    from app.services.planning_cognition_material import cognition_reference
    r = cognition_book; await cognition_material(r)
    body = await request_body(r, target=4, core_setup='林禾核对信的去向。')
    await submit(r, body)
    async def stop(*args): raise asyncio.CancelledError()
    r.before_call = stop
    with pytest.raises(asyncio.CancelledError): await run(r, body['request_id'])
    before = snapshot(r, body['request_id'])
    source, journal, intent = (deepcopy(before[key]) for key in ('source_json', 'journal_json', 'request_json'))
    source['cognition_context']['ledger']['林禾']['history'][-1]['evidence'] *= 2000
    journal['cognition_reference'] = cognition_reference(source['cognition_context'])
    journal['stages'][0]['attempts'][0]['cognition'].update(
        reference_hash=digest(journal['cognition_reference']), space_id='a' * 64)
    intent['expected_source'] = source_token(r.pid, source)
    with r.engine.begin() as db:
        db.execute(update(Request).where(Request.id == UUID(body['request_id'])).values(
            source_json=source, source_hash=digest(source), request_json=intent, request_hash=digest(intent),
            journal_json=journal, journal_hash=digest(journal)))
    async with r.client() as client:
        response = await client.get(path(r, f'/{body["request_id"]}/detail'))
    assert response.status_code == 200
    detail = response.json()
    assert not detail['integrity_valid'] and not detail['can_resume']
    assert detail['source'] == source and detail['journal'] == journal
    r.before_call = None
    await run(r, body['request_id'], manual=True)
    after = snapshot(r, body['request_id'])
    assert after['status'] == 'needs_review' and after['error_code'] == 'arc_planning_record_invalid'
    assert after['execution_attempts'] == before['execution_attempts'] and len(r.calls) == 1
    assert after['source_json'] == source and after['journal_json'] == journal
