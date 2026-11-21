"""Real SQLite source/receipt SQL, with explicit doubles for PG leases and graph IO.

These tests do not claim to verify PostgreSQL locks, sequences or commit recovery.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.sql.functions import next_value

from app.models.entity_extraction import ChapterEntityExtraction as Record
from app.services import entity_projection as ep
from app.services import relationship_scope_delivery as delivery
from app.services.relationship_scope import SYSTEM_PROMPT
from app.services.entity_sources import digest
from tests.relationship_graph_double import RelationshipGraph
from tests.test_character_state_context import state_db, add_state
from tests.test_relationship_context import reference
from tests.test_relationship_scope_projection import payload, project


@pytest_asyncio.fixture
async def workflow(state_db, monkeypatch):
    r = state_db
    r.first = await add_state(r, 900, {}, body='陈川和沈青是同事，也是朋友。')
    r.source = await add_state(r, 901, {}, body='陈川和沈青绝交了，公事照办。')
    # Preserve production column types; SQLite has no PG sequence/defaults.
    Table(Record.__tablename__, MetaData(), *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in Record.__table__.columns]).create(r.session.get_bind())
    r.rid = uuid4()
    r.session.add(Record(id=r.rid, project_id=r.pid, chapter_id=UUID(r.source.chapter_id),
        source_hash=r.source.source_hash, source_json=r.source.receipt, projection_token=1,
        projection_version=2, status='pending'))
    r.session.commit()
    r.graph = RelationshipGraph(r.pid)
    await project(r.graph, r.first, payload(r.first, new=['同事', '朋友']), 1)
    r.data = payload(r.source, changed=[('绝交', [reference(r.first)])])
    r.base = deepcopy(r.data)
    r.base['changed_relationships'][0].pop('replaces')
    execute = r.db.execute
    token = 10

    async def scalar(statement):
        nonlocal token
        if isinstance(statement, next_value):
            token += 1
            return token
        return (await execute(statement)).scalar()

    async def get(model, rid): return r.session.get(model, rid)
    async def commit(): r.session.commit()

    @asynccontextmanager
    async def sessions():
        db = NS(execute=execute, scalar=scalar, get=get, commit=commit, closed=False)
        try: yield db
        finally:
            r.session.rollback()
            db.closed = True

    @asynccontextmanager
    async def lease(*args): yield NS(check=AsyncMock())

    r.sessions = sessions
    r.scope_snapshot = NS(system_prompt=SYSTEM_PROMPT, endpoint_id=uuid4())
    monkeypatch.setattr('app.services.prompt_cache.get_snapshot', AsyncMock(return_value=r.scope_snapshot))
    r.materialize, r.cards = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ep, 'ensure_record', AsyncMock(return_value=r.rid))
    monkeypatch.setattr(ep, 'entity_project_lock', lease)
    return r


async def run(r, *, caller='scope-test', delivery_id=None):
    return await ep.run_entity_projection(source=r.source, sessions=r.sessions, engine=None,
        driver=r.graph, caller=caller, materialize=r.materialize, memory_cards=r.cards, delivery_id=delivery_id)


@pytest.mark.asyncio
async def test_reference_read_session_is_closed_before_model_call_and_scope_is_durable(workflow, monkeypatch):
    r = workflow
    original = delivery.verified_relationship_candidates
    read_sessions = []

    async def candidates(db, *args, **kwargs):
        read_sessions.append(db)
        return await original(db, *args, **kwargs)

    async def review(source, event, batch, db):
        assert read_sessions and all(db.closed for db in read_sessions)
        assert db not in read_sessions
        assert reference(r.first) in {row['record_id'] for row in batch['candidates']}
        row = r.session.get(Record, r.rid)
        assert row.payload_stage == 'scope_pending' and row.payload_json == r.base
        assert row.scope_json['batches'][0]['reviews'] is None
        return [dict(record_id=candidate['record_id'], ended=candidate['type'] == '朋友',
            evidence=source.content if candidate['type'] == '朋友' else '') for candidate in batch['candidates']]

    monkeypatch.setattr(delivery, 'verified_relationship_candidates', candidates)
    monkeypatch.setattr(ep, 'extract_entity_changes', AsyncMock(return_value=r.base))
    monkeypatch.setattr(delivery, 'review_scope_batch', AsyncMock(side_effect=review))
    assert (await run(r))['status'] == 'ok'
    row = r.session.get(Record, r.rid)
    assert row.payload_json == r.data and row.projection_version == 3 and row.status == 'completed'
    assert row.payload_stage == 'complete' and row.scope_json is None
    r.materialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_reference_source_keeps_initial_extraction_without_scope_or_projection(workflow, monkeypatch):
    r = workflow
    before = list(r.graph.markers)
    candidates = AsyncMock(side_effect=RuntimeError('graph offline'))
    monkeypatch.setattr(delivery, 'verified_relationship_candidates', candidates)
    model = AsyncMock(return_value=r.base)
    review = AsyncMock(side_effect=AssertionError('no source-verified scope input'))
    monkeypatch.setattr(delivery, 'review_scope_batch', review)
    monkeypatch.setattr(ep, 'extract_entity_changes', model)
    with pytest.raises(RuntimeError, match='graph offline'): await run(r)
    with pytest.raises(RuntimeError, match='graph offline'): await run(r)
    model.assert_awaited_once()
    review.assert_not_awaited()
    assert list(r.graph.markers) == before
    row = r.session.get(Record, r.rid)
    assert row.status == 'scope_failed' and row.payload_json == r.base and row.scope_json is None
    assert row.payload_stage == 'scope_pending'


@pytest.mark.asyncio
async def test_v2_completed_payload_upgrades_once_without_reextracting_or_new_reference_reads(workflow, monkeypatch):
    r = workflow
    legacy = payload(r.source, changed=[('绝交', None)])
    row = r.session.get(Record, r.rid)
    row.payload_json, row.payload_hash, row.status = legacy, digest([r.source.source_hash, legacy]), 'completed'
    r.session.commit()
    no_read = AsyncMock(side_effect=AssertionError('retained payload cannot acquire new end targets'))
    no_model = AsyncMock(side_effect=AssertionError('upgrade cannot call a model'))
    monkeypatch.setattr(delivery, 'verified_relationship_candidates', no_read)
    monkeypatch.setattr(ep, 'extract_entity_changes', no_model)
    assert (await run(r))['status'] == 'ok'
    assert (await run(r))['reason'] == 'already_completed'
    no_read.assert_not_awaited(); no_model.assert_not_awaited()
    r.materialize.assert_awaited_once()
    row = r.session.get(Record, r.rid)
    assert row.projection_version == 3 and row.payload_json == legacy


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', [False, True])
async def test_recovery_selects_v2_receipts_without_sending_corruption_to_a_model(workflow, monkeypatch, corrupt):
    r = workflow
    row = r.session.get(Record, r.rid)
    row.payload_json, row.payload_hash = r.data, 'corrupted' if corrupt else digest([r.source.source_hash, r.data])
    row.status, row.updated_at = 'completed', ep.now() - timedelta(minutes=2)
    r.session.commit()
    dispatched = []
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction',
                        lambda **kw: dispatched.append(kw) or True)
    result = await ep.replay_pending_extractions(r.sessions)
    assert result['examined'] == 1 and result['queued'] == (0 if corrupt else 1)
    assert len(dispatched) == (0 if corrupt else 1)
    row = r.session.get(Record, r.rid)
    if corrupt:
        assert row.status == 'failed' and row.payload_hash is None and row.last_error == 'InvalidRetainedPayload'
        assert (await ep.replay_pending_extractions(r.sessions))['examined'] == 0
    else:
        assert row.payload_json == r.data and row.projection_version == 2
