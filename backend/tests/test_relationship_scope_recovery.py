"""Resume paid batches using real SQLite receipts, with explicit PG/graph doubles."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update

from app.models.entity_extraction import ChapterEntityExtraction as Record
from app.models.project import Chapter
from app.services import entity_projection as ep, relationship_scope_delivery as delivery
from app.services.entity_context import verified_world_snapshot
from app.services.entity_graph_projection import ProjectionSuperseded
from app.services.entity_sources import digest, resolve_entity_source
from app.services.relationship_scope import RelationshipScopeError
from app.services.relationship_scope_delivery import extraction_payload_hash, InvalidRetainedPayload
from tests.test_character_state_context import state_db
from tests.test_relationship_context import reference
from tests.test_relationship_scope_projection import payload, project
from tests.test_relationship_scope_workflow import workflow, run


def response(source, batch):
    return [dict(record_id=row['record_id'], ended=row['type'] == '朋友',
                 evidence=source.content if row['type'] == '朋友' else '') for row in batch['candidates']]


async def many_relationships(r, monkeypatch):
    await project(r.graph, r.first, payload(r.first, new=['同事', '朋友'] + [f'约定{i}' for i in range(33)]), 2)
    model = AsyncMock(return_value=r.base)
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    return model


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_failed_or_cancelled_second_batch_resumes_without_reextracting_or_rereading(workflow, monkeypatch, cancelled):
    r = workflow
    model = await many_relationships(r, monkeypatch)
    before = deepcopy(r.graph.markers)
    attempts = []

    async def interrupted(source, event, batch, db):
        attempts.append([row['record_id'] for row in batch['candidates']])
        if len(attempts) == 2:
            raise asyncio.CancelledError() if cancelled else RuntimeError('scope provider failed')
        return response(source, batch)

    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=interrupted))
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError): await run(r)
    row = r.session.get(Record, r.rid)
    assert row.payload_stage == 'scope_pending' and row.payload_json == r.base
    assert row.status == ('running' if cancelled else 'scope_failed')
    frozen = deepcopy(row.scope_json)
    assert len(frozen['batches']) == 3
    assert [batch['reviews'] is not None for batch in frozen['batches']] == [True, False, False]
    assert row.payload_hash == extraction_payload_hash(r.source.source_hash, r.base, 'scope_pending', frozen)
    assert r.graph.markers == before
    r.materialize.assert_not_awaited()

    # A normal model failure never enters the periodic paid loop; only an
    # abandoned running worker can be recovered after its crash timeout.
    row.updated_at = ep.now() - timedelta(minutes=11)
    r.session.commit()
    queued = []
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', lambda **kw: queued.append(kw) or True)
    assert (await ep.replay_pending_extractions(r.sessions))['queued'] == int(cancelled)
    assert len(queued) == int(cancelled)

    resumed = []
    async def review(source, event, batch, db):
        resumed.append([row['record_id'] for row in batch['candidates']])
        return response(source, batch)

    no_read = AsyncMock(side_effect=AssertionError('frozen scope must not acquire new targets'))
    monkeypatch.setattr(delivery, 'verified_relationship_candidates', no_read)
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=review))
    assert (await run(r))['status'] == 'ok'
    assert resumed == [[row['record_id'] for row in batch['candidates']] for batch in frozen['batches'][1:]]
    assert not set(attempts[0]).intersection(rid for group in resumed for rid in group)
    model.assert_awaited_once(); no_read.assert_not_awaited()
    row = r.session.get(Record, r.rid)
    assert row.status == 'completed' and row.payload_stage == 'complete' and row.scope_json is None
    assert r.session.scalar(select(Record.id).where(Record.id == r.rid, Record.scope_json.is_(None))) == r.rid
    assert row.payload_json == r.data and row.payload_hash == digest([r.source.source_hash, r.data])


@pytest.mark.asyncio
async def test_edit_during_scope_stops_old_source_before_projection(workflow, monkeypatch):
    r = workflow
    await many_relationships(r, monkeypatch)
    before = deepcopy(r.graph.markers)
    calls = []

    async def edit(source, event, batch, db):
        calls.append(batch)
        if len(calls) == 2:
            r.session.execute(update(Chapter).where(Chapter.id == UUID(source.chapter_id)).values(content_text='作者修改了这一章。'))
            r.session.commit()
        return response(source, batch)

    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=edit))
    assert (await run(r))['reason'] == 'stale_source'
    assert len(calls) == 2 and r.graph.markers == before
    row = r.session.get(Record, r.rid)
    assert row.status == 'obsolete' and row.payload_stage == 'scope_pending'
    assert sum(batch['reviews'] is not None for batch in row.scope_json['batches']) == 1
    r.materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_fencing_token_cannot_accept_late_scope_output(workflow, monkeypatch):
    r = workflow
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    before = deepcopy(r.graph.markers)

    async def replaced(source, event, batch, db):
        r.session.execute(update(Record).where(Record.id == r.rid).values(projection_token=999, status='running'))
        r.session.commit()
        return response(source, batch)

    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=replaced))
    with pytest.raises(ProjectionSuperseded): await run(r)
    row = r.session.get(Record, r.rid)
    assert row.projection_token == 999 and row.status == 'running' and row.last_error is None
    assert row.scope_json['batches'][0]['reviews'] is None and r.graph.markers == before


@pytest.mark.asyncio
async def test_new_same_named_prior_record_is_not_substituted_for_the_frozen_reference(workflow, monkeypatch):
    r = workflow
    model = await many_relationships(r, monkeypatch)
    review = AsyncMock(side_effect=RuntimeError('temporarily unavailable'))
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    with pytest.raises(RuntimeError): await run(r)
    row = r.session.get(Record, r.rid)
    frozen_ids = {candidate['record_id'] for batch in row.scope_json['batches'] for candidate in batch['candidates']}
    r.session.execute(update(Chapter).where(Chapter.id == UUID(r.first.chapter_id)).values(content_text='作者重新写了前章的朋友关系。'))
    r.session.commit()
    newer = await resolve_entity_source(r.db, r.pid, 0, r.first.chapter_id)
    await project(r.graph, newer, payload(newer, new=['朋友']), 20)
    assert reference(newer) not in frozen_ids

    async def resume(source, event, batch, db):
        assert all(candidate['record_id'] in frozen_ids for candidate in batch['candidates'])
        return response(source, batch)

    monkeypatch.setattr(delivery, 'verified_relationship_candidates', AsyncMock(side_effect=AssertionError('no retargeting')))
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=resume))
    assert (await run(r))['status'] == 'ok'
    model.assert_awaited_once()
    snapshot = await verified_world_snapshot(r.db, r.graph, r.pid, 902)
    assert {rel.rel_type for rel in snapshot.relationships} == {'朋友', '绝交'}
    assert next(rel for rel in snapshot.relationships if rel.rel_type == '朋友').record_id == reference(newer)
    assert next(rel for rel in snapshot.relationships if rel.rel_type == '绝交').unresolved_replaces == [reference(r.first)]


@pytest.mark.asyncio
async def test_graph_read_model_failure_replays_complete_payload_without_any_scope_calls(workflow, monkeypatch):
    r = workflow
    model = AsyncMock(return_value=r.base)
    review = AsyncMock(side_effect=lambda source, event, batch, db: response(source, batch))
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    r.materialize.side_effect = [RuntimeError('read model unavailable'), None]
    with pytest.raises(RuntimeError): await run(r)
    row = r.session.get(Record, r.rid)
    assert row.status == 'failed' and row.payload_stage == 'complete' and row.scope_json is None
    row.updated_at = ep.now() - timedelta(minutes=2)
    r.session.commit()
    queued = []
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', lambda **kw: queued.append(kw) or True)
    assert (await ep.replay_pending_extractions(r.sessions))['queued'] == 1
    monkeypatch.setattr(delivery, 'verified_relationship_candidates', AsyncMock(side_effect=AssertionError('no new targets')))
    assert (await run(r))['status'] == 'ok'
    model.assert_awaited_once(); review.assert_awaited_once()
    assert r.materialize.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [False, True])
async def test_no_changes_or_no_prior_candidates_uses_no_scope_model(workflow, monkeypatch, changed):
    r = workflow
    data = deepcopy(r.base)
    if not changed: data['changed_relationships'] = []
    model = AsyncMock(return_value=data)
    read = AsyncMock(return_value=[])
    review = AsyncMock(side_effect=AssertionError('no scope model needed'))
    snapshot = AsyncMock(side_effect=AssertionError('no scope route needed'))
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    monkeypatch.setattr(delivery, 'verified_relationship_candidates', read)
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    monkeypatch.setattr('app.services.prompt_cache.get_snapshot', snapshot)
    assert (await run(r))['status'] == 'ok'
    assert read.await_count == int(changed)
    model.assert_awaited_once(); review.assert_not_awaited(); snapshot.assert_not_awaited()
    row = r.session.get(Record, r.rid)
    assert row.payload_stage == 'complete'
    if changed: assert row.payload_json['changed_relationships'][0]['replaces'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_prompt', 'unbound_prompt', 'oversized_system', 'oversized_late_candidate'])
async def test_plan_or_route_failure_keeps_initial_result_without_starting_any_batch(workflow, monkeypatch, failure):
    r = workflow
    model = AsyncMock(return_value=r.base)
    review = AsyncMock(side_effect=AssertionError('plan must be complete before scope IO'))
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    if failure == 'missing_prompt':
        monkeypatch.setattr('app.services.prompt_cache.get_snapshot', AsyncMock(return_value=None))
    elif failure == 'unbound_prompt': r.scope_snapshot.endpoint_id = None
    elif failure == 'oversized_system': r.scope_snapshot.system_prompt = '注册提示词' * 48000
    else:
        original = delivery.verified_relationship_candidates
        async def candidates(*args, **kwargs):
            result = await original(*args, **kwargs)
            result[-1]['type'] = '完整限定语。' * 48000
            return result
        monkeypatch.setattr(delivery, 'verified_relationship_candidates', candidates)
    with pytest.raises(RelationshipScopeError): await run(r)
    model.assert_awaited_once(); review.assert_not_awaited()
    row = r.session.get(Record, r.rid)
    assert row.payload_stage == 'scope_pending' and row.payload_json == r.base and row.scope_json is None
    assert row.status == 'scope_failed'
    r.materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['digest', 'marked_complete', 'stage', 'missing_base'])
async def test_invalid_partial_cache_never_reextracts_or_projects(workflow, monkeypatch, corruption):
    r = workflow
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=RuntimeError('pause after plan')))
    with pytest.raises(RuntimeError): await run(r)
    row = r.session.get(Record, r.rid)
    if corruption == 'digest': row.payload_hash = digest('other data')
    elif corruption == 'marked_complete': row.status = 'ready'
    elif corruption == 'stage': row.payload_stage, row.scope_json = 'complete', None
    else: row.payload_json = None
    r.session.commit()
    no_model = AsyncMock(side_effect=AssertionError('invalid cache cannot be replaced silently'))
    monkeypatch.setattr(ep, 'extract_entity_changes', no_model)
    with pytest.raises(InvalidRetainedPayload): await run(r)
    no_model.assert_not_awaited(); r.materialize.assert_not_awaited()
    row = r.session.get(Record, r.rid)
    assert row.status == 'failed' and row.payload_hash is None


@pytest.mark.asyncio
async def test_corrupt_crashed_partial_is_quarantined_before_periodic_dispatch(workflow, monkeypatch):
    r = workflow
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError): await run(r)
    row = r.session.get(Record, r.rid)
    broken = deepcopy(row.scope_json)
    broken['batches'] = []
    row.scope_json, row.updated_at = broken, ep.now() - timedelta(minutes=11)
    r.session.commit()
    queued = []
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', lambda **kw: queued.append(kw) or True)
    assert (await ep.replay_pending_extractions(r.sessions)) == {'examined': 1, 'queued': 0}
    assert not queued
    row = r.session.get(Record, r.rid)
    assert row.status == 'failed' and row.payload_hash is None and row.last_error == 'InvalidRetainedPayload'


@pytest.mark.asyncio
async def test_already_queued_periodic_delivery_cannot_restart_a_known_scope_failure(workflow, monkeypatch):
    r = workflow
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    review = AsyncMock(side_effect=RuntimeError('known scope model failure'))
    monkeypatch.setattr(delivery, 'review_scope_batch', review)

    async def periodic():
        return await ep.run_entity_projection(source=r.source, sessions=r.sessions, engine=None, driver=r.graph,
            caller='entity_projection_replay', materialize=r.materialize, memory_cards=r.cards)

    with pytest.raises(RuntimeError): await periodic()
    review.reset_mock()
    review.side_effect = lambda source, event, batch, db: response(source, batch)
    assert (await periodic())['reason'] == 'model_retry_requires_original_task'
    review.assert_not_awaited()
    assert r.session.get(Record, r.rid).status == 'scope_failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('retry', ['original', 'different', 'missing', 'explicit'])
async def test_only_original_periodic_task_or_explicit_retry_can_resume_failed_scope(workflow, monkeypatch, retry):
    r = workflow
    model = AsyncMock(return_value=r.base)
    review = AsyncMock(side_effect=RuntimeError('scope failed'))
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    with pytest.raises(RuntimeError):
        await run(r, caller='entity_projection_replay', delivery_id='original-job')
    assert r.session.get(Record, r.rid).delivery_id == 'original-job'
    review.reset_mock()
    review.side_effect = lambda source, event, batch, db: response(source, batch)
    delivery_id = {'original': 'original-job', 'different': 'new-periodic-job', 'missing': None, 'explicit': 'explicit-job'}[retry]
    result = await run(r, caller='manual_retry' if retry == 'explicit' else 'entity_projection_replay', delivery_id=delivery_id)
    allowed = retry in ('original', 'explicit')
    assert result['status'] == ('ok' if allowed else 'skipped')
    assert review.await_count == int(allowed)
    model.assert_awaited_once()
    row = r.session.get(Record, r.rid)
    assert row.delivery_id == (delivery_id if allowed else 'original-job')
    assert row.status == ('completed' if allowed else 'scope_failed')


@pytest.mark.asyncio
async def test_duplicate_periodic_job_also_cannot_restart_failed_initial_extraction(workflow, monkeypatch):
    r = workflow
    model = AsyncMock(side_effect=RuntimeError('initial model failed'))
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    with pytest.raises(RuntimeError):
        await run(r, caller='entity_projection_replay', delivery_id='original-job')
    row = r.session.get(Record, r.rid)
    assert row.status == 'failed' and row.payload_json is None
    model.reset_mock(); model.side_effect = None; model.return_value = r.base
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=lambda source, event, batch, db: response(source, batch)))
    assert (await run(r, caller='entity_projection_replay', delivery_id='queued-job'))['reason'] == 'model_retry_requires_original_task'
    model.assert_not_awaited()
    assert (await run(r, caller='entity_projection_replay', delivery_id='original-job'))['status'] == 'ok'
    model.assert_awaited_once()


@pytest.mark.asyncio
async def test_chapter_and_cascaded_receipt_deleted_during_scope_returns_stale_source(workflow, monkeypatch):
    r = workflow
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    before = deepcopy(r.graph.markers)
    async def remove(source, event, batch, db):
        # Model the result of PG's existing ON DELETE CASCADE explicitly;
        # SQLite here verifies the checkpoint SQL, not cascade enforcement.
        r.session.execute(delete(Record).where(Record.id == r.rid))
        r.session.execute(delete(Chapter).where(Chapter.id == UUID(source.chapter_id)))
        r.session.commit()
        return response(source, batch)
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=remove))
    assert (await run(r)) == {'status': 'skipped', 'reason': 'stale_source'}
    assert r.graph.markers == before
    r.materialize.assert_not_awaited()
