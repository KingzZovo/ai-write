"""Author handoff contracts on real SQLite SQL; no provider/broker IO."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, insert, select, update

from app.models.cognition import ChapterCognition as Record, CognitionRequest as Receipt, CognitionBackfill as Backfill
from app.models.project import Chapter, Project, Volume
from app.services.context_errors import RequiredContextUnavailable
from app.services import character_cognition as history
from app.services import cognition_extraction as extraction
from app.services import cognition_reader as reader
from app.services import cognition_requests as requests
from app.services import cognition_backfill as backfill
from app.services.entity_sources import resolve_entity_source, resolve_entity_sources, digest
from tests.test_cognition_workflow_offline import cognition_db, state_db, run, stored, aged_record, add_state


@pytest.mark.asyncio
async def test_new_delivery_alone_cannot_reset_a_failed_cycle(cognition_db):
    r = cognition_db
    r.model.side_effect = RuntimeError('provider unavailable')
    for _ in range(extraction.MAX_EXTRACTION_ATTEMPTS):
        with pytest.raises(RuntimeError):
            await run(r, delivery_id='original', recovery=True)
    r.model.side_effect = None
    result = await run(r, delivery_id='unrelated-delivery')
    assert result['status'] == 'skipped'
    assert stored(r).attempt_count == extraction.MAX_EXTRACTION_ATTEMPTS
    assert r.model.await_count == extraction.MAX_EXTRACTION_ATTEMPTS


@pytest.mark.asyncio
async def test_never_extracted_prior_chapter_is_not_empty_knowledge(cognition_db):
    with pytest.raises(RequiredContextUnavailable, match='not_ready'):
        await history.load_ledger(cognition_db.db, cognition_db.pid, before_global_idx=901)


async def view(r):
    return await reader.read_chapter_cognition(r.db, r.pid, r.source.chapter_id)


async def retry(r, *, request_id=None, snapshot=None, project_id=None, chapter_id=None):
    snapshot = snapshot or await view(r)
    async with r.sessions() as db:
        result = await requests.retry_cognition(db, project_id or r.pid, chapter_id or r.source.chapter_id,
            request_id or uuid4(), snapshot['source_hash'], snapshot['version'])
        await db.commit()
        return result


async def schedule(r, request_id=None):
    async with r.sessions() as db:
        result = await backfill.create_backfill(db, r.pid, request_id or uuid4())
        await db.commit()
        return result


def backfill_row(r, job_id):
    r.session.expire_all()
    return r.session.get(Backfill, UUID(str(job_id)))


@pytest.mark.asyncio
async def test_author_retry_has_new_cycle_and_fixed_receipt_after_completion(cognition_db):
    r = cognition_db
    await aged_record(r, 'failed')
    r.session.execute(update(Record).values(attempt_count=4, delivery_id='old'))
    r.session.commit()
    before = await view(r)
    original_cycle = stored(r).cycle_id
    request_id = uuid4()
    accepted = await retry(r, request_id=request_id, snapshot=before)
    row = stored(r)
    assert row.cycle_id != original_cycle and str(row.cycle_id) == accepted['cycle_id']
    assert row.attempt_count == 0 and row.status == 'pending' and row.delivery_id is None
    for options in ({}, {'cycle_id': str(original_cycle)}):
        assert (await run(r, delivery_id='old', **options))['status'] == 'superseded'
    r.model.assert_not_awaited()
    assert (await run(r, delivery_id='new', cycle_id=accepted['cycle_id']))['status'] == 'completed'
    assert await retry(r, request_id=request_id, snapshot=before) == accepted
    assert stored(r).status == 'completed' and stored(r).attempt_count == 1
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_request_is_idempotent_and_dispatch_carries_cycle(cognition_db):
    r = cognition_db
    request_id, before = uuid4(), await view(r)
    accepted = await retry(r, request_id=request_id, snapshot=before)
    assert stored(r).cycle_id == stored(r).id  # Compatibility with initial messages.
    published = []
    assert (await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *args: published.append(args)))['dispatched'] == 1
    assert published == [(str(r.pid), r.source.chapter_id, r.source.source_hash, accepted['cycle_id'])]
    assert await retry(r, request_id=request_id, snapshot=before) == accepted
    assert stored(r).status == 'queued' and stored(r).attempt_count == 0


@pytest.mark.asyncio
# 2026-11-28:status 从"陈旧"参数里移出。source 身份早已不含 status(评审态是展示
# 字段,见 entity_sources._receipt_from_row),因此评审态翻转不该让提取请求失效;
# 且 `resolve_cognition_source` 现在对"有正文但被降级"的章同样解析,于是这条不再
# 冲突 —— 反而符合"正文存在即事实存在"。正文/章序/章名/删除/尝试数仍然是陈旧。
@pytest.mark.parametrize('change', ['body', 'order', 'title', 'deleted', 'attempt'])
async def test_stale_author_request_does_not_reset_or_ack(cognition_db, change):
    r = cognition_db
    await aged_record(r, 'failed')
    before = await view(r)
    if change == 'attempt': r.session.execute(update(Record).values(attempt_count=3))
    elif change == 'deleted': r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
    else:
        field, value = {'body': ('content_text', '新的保存稿。'), 'order': ('global_idx', 4),
            'status': ('status', 'needs_review'), 'title': ('title', '新的章名')}[change]
        r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(**{field: value}))
    r.session.commit()
    with pytest.raises(requests.CognitionConflict): await retry(r, snapshot=before)
    assert r.session.scalar(select(Receipt.id)) is None
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_survives_chapter_deletion_and_cannot_change_scope(cognition_db):
    r = cognition_db
    before, request_id = await view(r), uuid4()
    accepted = await retry(r, request_id=request_id, snapshot=before)
    r.session.execute(delete(Record))
    r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
    r.session.commit()
    assert await retry(r, request_id=request_id, snapshot=before) == accepted
    with pytest.raises(requests.CognitionConflict):
        await retry(r, request_id=request_id, snapshot=before, chapter_id=uuid4())
    pid2 = uuid4()
    r.session.execute(insert(Project).values(id=pid2, title='另一部作品')); r.session.commit()
    with pytest.raises(requests.CognitionConflict):
        await retry(r, request_id=request_id, snapshot=before, project_id=pid2)
    assert r.session.scalar(select(Record.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'queued', 'running', 'completed'])
async def test_request_cannot_force_restart_active_or_valid_completed_work(cognition_db, state):
    r = cognition_db
    if state == 'completed': await run(r)
    else: await aged_record(r, state)
    cycle, attempts = stored(r).cycle_id, stored(r).attempt_count
    accepted = await retry(r)
    assert accepted['outcome'] == ('already_complete' if state == 'completed' else 'already_active')
    assert stored(r).cycle_id == cycle and stored(r).attempt_count == attempts and stored(r).status == state


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['payload', 'quote', 'receipt'])
async def test_invalid_result_is_visible_and_can_be_reextracted(cognition_db, corruption):
    r = cognition_db
    await run(r)
    original = stored(r).cycle_id
    if corruption == 'payload': r.session.execute(update(Record).values(payload_hash='bad'))
    elif corruption == 'receipt': r.session.execute(update(Record).values(source_json={}))
    else:
        changes = [dict(r.changes[0], evidence='无关的原句')]
        r.session.execute(update(Record).values(changes_json=changes, payload_hash=digest([r.source.source_hash, changes])))
    r.session.commit()
    detail = await view(r)
    assert detail['status'] == 'invalid' and detail['can_retry'] and detail['changes'] is None
    accepted = await retry(r, snapshot=detail)
    assert stored(r).cycle_id != original and stored(r).changes_json is None
    assert (await run(r, cycle_id=accepted['cycle_id']))['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('model_result', ['valid', 'invalid', 'failure'])
async def test_in_flight_old_cycle_cannot_complete_or_repair_after_author_retry(cognition_db, model_result):
    r = cognition_db
    accepted = None
    async def model(**kwargs):
        nonlocal accepted
        # Simulate a timed-out/crashed execution becoming author-retryable
        # while its external request still has a late response in flight.
        r.session.execute(update(Record).values(status='failed'))
        r.session.commit()
        accepted = await retry(r)
        if model_result == 'failure': raise RuntimeError('late provider error')
        return NS(text=json.dumps(r.changes) if model_result == 'valid' else 'invalid', finish_reason='stop')
    r.model.side_effect = model
    if model_result == 'failure':
        with pytest.raises(RuntimeError): await run(r)
    else:
        assert (await run(r))['status'] == 'superseded'
    assert stored(r).status == 'pending' and str(stored(r).cycle_id) == accepted['cycle_id']
    assert stored(r).changes_json is None and stored(r).attempt_count == 0
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_after_obsolete_allocates_cycle_and_fences_legacy_message(cognition_db):
    r = cognition_db
    await aged_record(r, 'obsolete')
    original_cycle = stored(r).cycle_id
    async with r.sessions() as db:
        await extraction.enqueue_cognition(db, r.source)
        await db.commit()
    assert stored(r).cycle_id != original_cycle and stored(r).status == 'pending'
    assert (await run(r))['status'] == 'superseded'
    assert (await run(r, cycle_id=str(stored(r).cycle_id)))['status'] == 'completed'


@pytest.mark.asyncio
async def test_completed_empty_is_distinct_from_missing_and_quotes_are_complete(cognition_db):
    r = cognition_db
    assert (await view(r))['status'] == 'missing'
    await run(r)
    detail = await view(r)
    assert detail['changes'] == r.changes and detail['changes_count'] == 1
    row = stored(r); row.changes_json = []; row.payload_hash = digest([r.source.source_hash, []]); r.session.commit()
    detail = await view(r)
    assert detail['status'] == 'completed' and detail['changes'] == [] and detail['changes_count'] == 0
    assert await history.load_ledger(r.db, r.pid, before_global_idx=901) == {}


@pytest.mark.asyncio
async def test_list_uses_current_source_and_safe_error_and_project_scope(cognition_db):
    r = cognition_db
    await aged_record(r, 'failed')
    r.session.execute(update(Record).values(last_error='private-provider-error'))
    r.session.commit()
    result = await reader.list_chapter_cognition(r.db, r.pid)
    assert 'private-provider-error' not in json.dumps(result)
    assert result['chapters'][0]['status'] == 'failed'
    r.session.execute(update(Chapter).values(content_text='现在的保存稿。')); r.session.commit()
    result = await reader.list_chapter_cognition(r.db, r.pid)
    assert result['chapters'][0]['status'] == 'missing' and result['chapters'][0]['attempts'] == 0
    with pytest.raises(reader.CognitionNotFound):
        await reader.read_chapter_cognition(r.db, uuid4(), r.source.chapter_id)


@pytest.mark.asyncio
async def test_pagination_uses_batched_source_queries_and_detects_duplicate_outside_page(cognition_db):
    r = cognition_db
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i, global_idx=899+i,
        title=f'第{i}章', status='completed', content_text='本章保存稿。') for i in range(2, 52)]
    r.session.execute(insert(Chapter), rows); r.session.commit()
    r.queries.clear()
    first = await reader.list_chapter_cognition(r.db, r.pid, limit=20)
    assert len(first['chapters']) == 20 and first['total'] == 51 and first['has_more']
    assert len(r.queries) <= 6
    second = await reader.list_chapter_cognition(r.db, r.pid, limit=20, offset=20)
    assert not {c['chapter_id'] for c in first['chapters']} & {c['chapter_id'] for c in second['chapters']}
    r.session.execute(update(Chapter).where(Chapter.id == rows[-1]['id']).values(global_idx=900)); r.session.commit()
    first = await reader.list_chapter_cognition(r.db, r.pid, limit=1)
    assert first['chapters'][0]['status'] == 'unavailable'
    assert await resolve_entity_source(r.db, r.pid, 0, r.source.chapter_id) is None


@pytest.mark.asyncio
async def test_batch_receipts_are_identical_to_single_source_resolver(cognition_db):
    r = cognition_db
    second = await add_state(r, 901, {})
    sources = await resolve_entity_sources(r.db, r.pid, [r.source.chapter_id, second.chapter_id, uuid4()])
    assert sources == {r.source.chapter_id: r.source, second.chapter_id: second}


@pytest.mark.asyncio
async def test_backfill_frozen_membership_and_deleted_or_unfinished_chapters(cognition_db):
    r = cognition_db
    deleted = await add_state(r, 901, {})
    changed = await add_state(r, 902, {})
    accepted = await schedule(r)
    assert accepted['chapter_count'] == 3
    assert await backfill.advance_backfills(r.sessions, batch_size=1) == 1
    later = await add_state(r, 903, {})
    r.session.execute(delete(Chapter).where(Chapter.id == UUID(deleted.chapter_id)))
    r.session.execute(update(Chapter).where(Chapter.id == UUID(changed.chapter_id)).values(status='draft'))
    r.session.commit()
    assert await backfill.advance_backfills(r.sessions, batch_size=1) == 1
    assert await backfill.advance_backfills(r.sessions, batch_size=1) == 1
    job = backfill_row(r, accepted['backfill_id'])
    assert job.status == 'scheduled' and job.cursor == 3
    assert job.added_count == 1 and job.unavailable_count == 2
    assert later.chapter_id not in job.chapter_ids
    assert stored(r).status == 'pending'  # Arrangement is not extraction.
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reuses_active_work_and_request_even_after_scheduling(cognition_db):
    r = cognition_db
    request_id = uuid4()
    first = await schedule(r, request_id)
    assert (await schedule(r))['backfill_id'] == first['backfill_id']
    await backfill.advance_backfills(r.sessions)
    assert await schedule(r, request_id) == first
    later = await schedule(r)
    assert later['backfill_id'] != first['backfill_id']
    await backfill.advance_backfills(r.sessions)
    job = backfill_row(r, later['backfill_id'])
    assert job.existing_count == 1 and job.added_count == 0
    assert stored(r).attempt_count == 0


@pytest.mark.asyncio
async def test_backfill_does_not_restart_failed_or_invalid_results(cognition_db):
    r = cognition_db
    await aged_record(r, 'failed')
    original = stored(r).cycle_id
    good = await add_state(r, 901, {}, body='无人到访。')
    r.model.return_value = NS(text='[]', finish_reason='stop')
    await extraction.run_cognition_extraction(good, sessions=r.sessions, engine=None)
    await add_state(r, 902, {})
    accepted = await schedule(r)
    await backfill.advance_backfills(r.sessions)
    job = backfill_row(r, accepted['backfill_id'])
    assert job.added_count == job.existing_count == job.attention_count == 1
    row = r.session.scalar(select(Record).where(Record.chapter_id == UUID(r.source.chapter_id)))
    assert row.status == 'failed' and row.cycle_id == original
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_crash_rolls_back_cursor_and_queue_together(cognition_db, monkeypatch):
    r = cognition_db
    accepted = await schedule(r)
    real = extraction.enqueue_cognition
    async def interrupted(db, source):
        await real(db, source)
        raise RuntimeError('simulated process failure')
    monkeypatch.setattr(extraction, 'enqueue_cognition', interrupted)
    with pytest.raises(RuntimeError): await backfill.advance_backfills(r.sessions)
    assert backfill_row(r, accepted['backfill_id']).cursor == 0
    assert r.session.scalar(select(Record.id)) is None
    monkeypatch.setattr(extraction, 'enqueue_cognition', real)
    assert await backfill.advance_backfills(r.sessions) == 1
    assert backfill_row(r, accepted['backfill_id']).cursor == 1


@pytest.mark.asyncio
async def test_full_snapshot_has_no_small_book_cap_and_handoff_is_bounded(cognition_db):
    r = cognition_db
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i, global_idx=899+i,
        title=f'第{i}章', status='completed', content_text='本章保存稿。') for i in range(2, 1006)]
    r.session.execute(insert(Chapter), rows); r.session.commit()
    accepted = await schedule(r)
    assert accepted['chapter_count'] == 1005
    sizes = []
    while True:
        size = await backfill.advance_backfills(r.sessions)
        if not size: break
        sizes.append(size)
    assert sizes == [100] * 10 + [5]
    job = backfill_row(r, accepted['backfill_id'])
    assert job.cursor == job.added_count == 1005 and job.status == 'scheduled'
    assert len(r.session.scalars(select(Record.id)).all()) == 1005
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_coverage_requires_every_prior_source_but_not_future_or_draft(cognition_db):
    r = cognition_db
    await run(r)
    later = await add_state(r, 901, {})
    assert '陈川' in await history.load_ledger(r.db, r.pid, before_global_idx=901)
    with pytest.raises(RequiredContextUnavailable, match='not_ready'):
        await history.load_ledger(r.db, r.pid, before_global_idx=902)
    r.session.execute(update(Chapter).where(Chapter.id == UUID(later.chapter_id)).values(status='draft')); r.session.commit()
    assert '陈川' in await history.load_ledger(r.db, r.pid, before_global_idx=902)


@pytest.mark.asyncio
async def test_routes_commit_before_ack_and_replay_after_lost_ack(cognition_db):
    from app.api import cognition as api
    r = cognition_db
    async def commit(): r.session.commit()
    async def rollback(): r.session.rollback()
    db = NS(execute=r.db.execute, commit=AsyncMock(side_effect=commit), rollback=AsyncMock(side_effect=rollback))
    async def provide_db(): return db
    app = FastAPI(); app.include_router(api.router); app.dependency_overrides[api.get_db] = provide_db
    base = f'/api/projects/{r.pid}/cognition'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        listing = await client.get(f'{base}/chapters')
        chapter = listing.json()['chapters'][0]
        payload = dict(request_id=str(uuid4()), source_hash=chapter['source_hash'], version=chapter['version'])
        url = f'{base}/chapters/{r.source.chapter_id}/retry'
        async def lost_ack():
            r.session.commit()
            raise RuntimeError('connection lost after commit')
        db.commit.side_effect = lost_ack
        response = await client.post(url, json=payload)
        assert response.status_code == 503
        first_cycle = stored(r).cycle_id
        db.commit.side_effect = commit
        response = await client.post(url, json=payload)
        assert response.status_code == 200 and response.json()['request_id'] == payload['request_id']
        assert stored(r).cycle_id == first_cycle and stored(r).attempt_count == 0
        assert db.commit.await_count == 2
        response = await client.post(url, json={**payload, 'request_id': str(uuid4())})
        assert response.status_code == 409  # Original missing-state version is stale.
        response = await client.get(f'{base}/chapters/{r.source.chapter_id}')
        assert response.status_code == 200 and response.json()['changes'] is None
        response = await client.post(f'{base}/backfills', json={'request_id': str(uuid4())})
        assert response.status_code == 200 and response.json()['chapter_count'] == 1
        response = await client.get(f'{base}/backfills')
        assert response.status_code == 200 and len(response.json()['backfills']) == 1
        response = await client.get(f'{base}/chapters?limit=101')
        assert response.status_code == 422
        response = await client.post(url, json={**payload, 'source_hash': 'bad'})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_author_cycle_migration_preserves_existing_deltas_and_attempts(cognition_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r = cognition_db
    await run(r, delivery_id='original')
    before = stored(r)
    expected = (before.id, before.source_hash, before.changes_json, before.payload_hash, before.attempt_count)
    spec = importlib.util.spec_from_file_location('cognition_author_migration',
        Path(__file__).parents[1] / 'alembic/versions/a1001947_cognition_author_requests.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    r.session.commit()
    with r.session.get_bind().begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade(); migration.upgrade()
    after = stored(r)
    assert (after.id, after.source_hash, after.changes_json, after.payload_hash, after.attempt_count) == expected
    assert after.cycle_id == after.id and after.delivery_id == 'original'


@pytest.mark.asyncio
async def test_backfill_and_retry_lock_parent_before_children(cognition_db):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql import Select
    r = cognition_db
    accepted = await schedule(r)
    r.queries.clear()
    await backfill.advance_backfills(r.sessions)
    locking = [str(q.compile(dialect=postgresql.dialect())) for q in r.queries
               if isinstance(q, Select) and 'FOR ' in str(q.compile(dialect=postgresql.dialect()))]
    assert 'FOR KEY SHARE OF projects SKIP LOCKED' in locking[0]
    assert 'cognition_backfills' in locking[1] and 'FOR UPDATE' in locking[1]
    assert backfill_row(r, accepted['backfill_id']).status == 'scheduled'
    r.queries.clear()
    await retry(r)
    locking = [str(q.compile(dialect=postgresql.dialect())) for q in r.queries
               if isinstance(q, Select) and 'FOR ' in str(q.compile(dialect=postgresql.dialect()))]
    assert 'projects' in locking[0] and 'FOR KEY SHARE' in locking[0]


@pytest.mark.asyncio
async def test_soft_deleted_project_cannot_start_or_advance_cognition(cognition_db):
    from app.services.entity_projection import now
    r = cognition_db
    accepted = await schedule(r)
    before = await view(r)
    r.session.execute(update(Project).values(deleted_at=now())); r.session.commit()
    with pytest.raises(reader.CognitionNotFound): await retry(r, snapshot=before)
    assert await backfill.advance_backfills(r.sessions) == 0
    assert backfill_row(r, accepted['backfill_id']).cursor == 0
    assert (await run(r))['status'] == 'obsolete'
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_routes_require_existing_authentication_middleware(cognition_db, monkeypatch):
    from app.api import cognition as api
    from app.main import AuthMiddleware
    monkeypatch.setenv('DISABLE_AUTH', '0')
    app = FastAPI(); app.include_router(api.router); app.add_middleware(AuthMiddleware)
    async def forbidden_db(): raise AssertionError('unauthenticated request reached database')
    app.dependency_overrides[api.get_db] = forbidden_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        base = f'/api/projects/{cognition_db.pid}/cognition'
        assert (await client.get(f'{base}/chapters')).status_code == 401
        assert (await client.post(f'{base}/backfills', json={'request_id': str(uuid4())})).status_code == 401


def test_generation_error_points_author_to_cognition_workflow_without_raw_error():
    from app.services.generation_errors import generation_error_payload
    result = generation_error_payload(RequiredContextUnavailable('character_cognition_not_ready'), 'chapter')
    assert result['code'] == 'chapter_context_unavailable'
    assert '人物认知' in result['error'] and '补提取缺失章节' in result['error']
    assert 'character_cognition_not_ready' not in result['error']


@pytest.mark.asyncio
async def test_lease_lost_during_source_recheck_does_not_call_model(cognition_db):
    from contextlib import asynccontextmanager
    r = cognition_db
    sessions = r.sessions
    count = 0
    @asynccontextmanager
    async def lose_on_check_close():
        nonlocal count
        count += 1
        this_call = count
        async with sessions() as db:
            yield db
        if this_call == 3:  # First pre-model source check has just closed.
            r.lease.check.side_effect = RuntimeError('lease lost while reading source')
    with pytest.raises(RuntimeError, match='lease lost'):
        await extraction.run_cognition_extraction(r.source, sessions=lose_on_check_close, engine=None)
    r.model.assert_not_awaited()
    assert stored(r).status == 'failed'


@pytest.mark.asyncio
async def test_reordering_between_snapshot_and_source_read_cannot_leak_cached_future(cognition_db, monkeypatch):
    from app.services import entity_sources
    r = cognition_db
    await run(r)
    r.session.execute(update(Chapter).values(global_idx=902)); r.session.commit()
    future = await resolve_entity_source(r.db, r.pid, 0, r.source.chapter_id)
    await extraction.run_cognition_extraction(future, sessions=r.sessions, engine=None)
    r.session.execute(update(Chapter).values(global_idx=900)); r.session.commit()
    actual = entity_sources.resolve_entity_sources
    async def move_before_read(db, project_id, ids, **kwargs):
        r.session.execute(update(Chapter).values(global_idx=902)); r.session.commit()
        return await actual(db, project_id, ids, **kwargs)
    monkeypatch.setattr(entity_sources, 'resolve_entity_sources', move_before_read)
    with pytest.raises(RequiredContextUnavailable, match='boundary'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
async def test_prior_membership_change_during_read_cannot_be_silently_omitted(cognition_db, monkeypatch):
    r = cognition_db
    await run(r)
    actual = reader.load_source_records
    inserted = False
    async def insert_prior(db, project_id, sources, **kwargs):
        nonlocal inserted
        records = await actual(db, project_id, sources, **kwargs)
        if not inserted:
            inserted = True
            await add_state(r, 899, {}, body='此前另有一章。')
        return records
    monkeypatch.setattr(reader, 'load_source_records', insert_prior)
    with pytest.raises(RequiredContextUnavailable, match='boundary'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
async def test_target_boundary_change_during_read_requires_rebuild(cognition_db, monkeypatch):
    r = cognition_db
    await run(r)
    target = await add_state(r, 901, {}, body='此章是评审目标。')
    actual = reader.load_source_records
    async def move_target(db, project_id, sources, **kwargs):
        records = await actual(db, project_id, sources, **kwargs)
        r.session.execute(update(Chapter).where(Chapter.id == UUID(target.chapter_id)).values(global_idx=902))
        r.session.commit()
        return records
    monkeypatch.setattr(reader, 'load_source_records', move_target)
    with pytest.raises(RequiredContextUnavailable, match='boundary'):
        await history.load_ledger(r.db, r.pid, before_chapter_id=target.chapter_id)


@pytest.mark.asyncio
async def test_body_edit_after_empty_delta_read_invalidates_snapshot(cognition_db, monkeypatch):
    r = cognition_db
    r.model.return_value = NS(text='[]', finish_reason='stop')
    await run(r)
    actual = reader.load_source_records
    async def edit_body(db, project_id, sources, **kwargs):
        records = await actual(db, project_id, sources, **kwargs)
        r.session.execute(update(Chapter).values(content_text='作者已更换正文，旧空结果不再证明本章无变化。'))
        r.session.commit()
        return records
    monkeypatch.setattr(reader, 'load_source_records', edit_body)
    with pytest.raises(RequiredContextUnavailable, match='source_unavailable'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
async def test_review_status_flip_still_allows_extraction(cognition_db):
    """评审态翻转不该让这一章变成"永久认知缺口"。

    2026-11-28 实测的《白蜡树下》回路:章被降级为 ``needs_review`` 后,提取侧只认
    ``completed`` → 它的认知永远建不起来 → 台账每章都少一段事实 → 后续章在缺前文
    的条件下写 → 更差 → 更多 ``needs_review``。第 450 章时已丢 258 章事实。
    """
    r = cognition_db
    await aged_record(r, 'failed')
    before = await view(r)
    r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id))
                      .values(status='needs_review'))
    r.session.commit()

    accepted = await retry(r, snapshot=before)

    assert accepted is not None, '正文没变、只有评审态变了,不该判为陈旧请求'
    assert stored(r).status in ('queued', 'pending')
    assert stored(r).attempt_count == 0
