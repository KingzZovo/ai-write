"""Real SQLite cognition receipts/chronology; PG leases and broker are doubles.

This cannot verify PostgreSQL row locks, advisory locks or broker delivery.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.cognition import ChapterCognition as Record, CognitionRequest, CognitionBackfill, CognitionRevision
from app.models.project import Chapter
from app.services import character_cognition as history
from app.services import cognition_extraction as extraction
from app.services.entity_sources import digest
from app.services.prompt_registry import run_text_prompt as actual_run_text_prompt
from tests.test_character_state_context import state_db, add_state


@pytest_asyncio.fixture
async def cognition_db(state_db, monkeypatch):
    r = state_db
    Record.__table__.create(r.session.get_bind())
    CognitionRequest.__table__.create(r.session.get_bind())
    CognitionBackfill.__table__.create(r.session.get_bind())
    CognitionRevision.__table__.create(r.session.get_bind())
    r.source = await add_state(r, 900, {}, body='陈川不知道信已送到。\n沈青把回执交给他，他才明白父亲已经收信。')
    execute = r.db.execute

    async def scalar(statement): return (await execute(statement)).scalar()
    async def get(model, key): return r.session.get(model, UUID(str(key)))
    async def commit(): r.session.commit()
    r.opened = []

    @asynccontextmanager
    async def sessions():
        db = NS(execute=execute, scalar=scalar, get=get, commit=commit)
        r.opened.append(db)
        try: yield db
        finally:
            r.session.rollback()
            r.opened.remove(db)

    # Only adapt the PostgreSQL named ON CONFLICT target to its identical
    # SQLite column target; execute the actual CASE/UPDATE and receipt SQL.
    class Insert:
        def __init__(self, table): self.table = table
        def values(self, **values): self.values_dict = values; return self
        def on_conflict_do_update(self, *, constraint, set_):
            assert constraint == 'uq_chapter_cognition_source'
            return sqlite_insert(self.table).values(**self.values_dict).on_conflict_do_update(
                index_elements=['chapter_id', 'source_hash'], set_=set_)

    r.lease = NS(check=AsyncMock())

    @asynccontextmanager
    async def lease(*args): yield r.lease

    r.sessions = sessions
    r.db.scalar = scalar
    monkeypatch.setattr(extraction, 'insert', Insert)
    monkeypatch.setattr(extraction, 'entity_project_lock', lease)
    r.changes = [dict(character='陈川', learns='父亲已经收信。', evidence='沈青把回执交给他，他才明白父亲已经收信。')]
    r.model = AsyncMock(return_value=NS(text=json.dumps(r.changes, ensure_ascii=False), finish_reason='stop'))
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', r.model)
    return r


async def run(r, **kwargs):
    return await extraction.run_cognition_extraction(r.source, sessions=r.sessions, engine=None, **kwargs)


def stored(r):
    r.session.expire_all()
    return r.session.scalar(select(Record))


async def aged_record(r, status):
    async with r.sessions() as db:
        await extraction.enqueue_cognition(db, r.source)
        await db.execute(update(Record).values(status=status, updated_at=extraction.now() - timedelta(minutes=6)))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'max_tokens', 'content_filter', 'tool_calls', 'unknown'])
async def test_explicit_incomplete_json_array_is_never_committed(cognition_db, reason):
    r = cognition_db
    r.model.return_value = NS(text='[]', finish_reason=reason)
    with pytest.raises(ValueError, match='provider_incomplete_response'):
        await run(r)
    row = stored(r)
    assert row.status == 'failed' and row.changes_json is None and row.payload_hash is None
    r.model.assert_awaited_once()  # No format-repair call on a provider ending.


@pytest.mark.asyncio
async def test_full_source_and_single_registered_prompt_outside_transactions(cognition_db):
    r = cognition_db
    async def model(**kwargs):
        assert not r.opened
        assert r.source.content in kwargs['user_content']
        assert kwargs['db'] is None and kwargs.get('extra_system', '') == ''
        assert 'max_tokens' not in kwargs
        return NS(text=json.dumps(r.changes, ensure_ascii=False), finish_reason='stop')
    r.model.side_effect = model
    assert (await run(r))['status'] == 'completed'
    assert (await run(r))['reused'] is True
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_known_model_failure_is_not_periodically_redispatched(cognition_db):
    r = cognition_db
    r.model.return_value = NS(text='malformed', finish_reason='stop')
    with pytest.raises(ValueError): await run(r)
    assert r.model.await_count == 2
    await aged_record(r, 'failed')
    published = []
    for _ in range(3):
        result = await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *a: published.append(a))
        assert result['dispatched'] == 0
    assert not published and stored(r).status == 'failed'


@pytest.mark.asyncio
async def test_late_periodic_delivery_cannot_take_over_another_delivery_failure(cognition_db):
    r = cognition_db
    r.model.side_effect = RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError): await run(r, recovery=True, delivery_id='original')
    result = await run(r, recovery=True, delivery_id='already-queued-periodic')
    assert result['reason'] == 'model_retry_requires_original_task'
    r.model.assert_awaited_once()
    assert stored(r).delivery_id == 'original'
    r.model.side_effect = None
    assert (await run(r, recovery=True, delivery_id='original'))['status'] == 'completed'
    assert r.model.await_count == 2


@pytest.mark.asyncio
async def test_new_delivery_requires_author_request_to_recover_failed_source(cognition_db):
    r = cognition_db
    r.model.side_effect = RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError): await run(r, delivery_id='original')
    r.model.side_effect = None
    assert (await run(r, delivery_id='unrelated-new'))['reason'] == 'model_retry_requires_original_task'
    assert stored(r).delivery_id == 'original'
    r.model.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'queued', 'running'])
async def test_unacknowledged_work_is_still_recoverable(cognition_db, status):
    r = cognition_db
    await aged_record(r, status)
    published = []
    result = await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *a: published.append(a))
    assert result['dispatched'] == 1 and published[0][1] == r.source.chapter_id
    assert (await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *a: published.append(a)))['dispatched'] == 0


@pytest.mark.asyncio
async def test_author_change_stops_format_repair_and_keeps_source_obsolete(cognition_db):
    r = cognition_db
    async def model(**kwargs):
        r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(content_text='作者新的正文。'))
        r.session.commit()
        return NS(text='invalid json', finish_reason='stop')
    r.model.side_effect = model
    result = await run(r)
    assert result['status'] == 'obsolete'
    r.model.assert_awaited_once()
    assert stored(r).status == 'obsolete' and stored(r).changes_json is None


@pytest.mark.asyncio
async def test_deleted_source_before_start_does_not_create_or_call(cognition_db):
    r = cognition_db
    r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
    r.session.commit()
    assert (await run(r))['status'] == 'obsolete'
    assert stored(r) is None
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_quotes_survive_actual_ledger_and_prompt(cognition_db):
    r = cognition_db
    await run(r)
    ledger = await history.load_ledger(r.db, r.pid, before_global_idx=901)
    event = ledger['陈川']['history'][0]
    assert event['evidence'] == r.changes[0]['evidence']
    assert event['source_chapter_id'] == r.source.chapter_id and event['source_hash'] == r.source.source_hash
    prompt = history.serialize_for_prompt(ledger)
    assert r.changes[0]['evidence'] in prompt and '[CH-900]' in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['digest', 'receipt', 'shape', 'quote'])
async def test_current_corrupt_record_cannot_silently_become_empty_knowledge(cognition_db, corruption):
    from app.services.context_errors import RequiredContextUnavailable
    r = cognition_db
    await run(r)
    row = stored(r)
    if corruption == 'digest': row.payload_hash = '0' * 64
    elif corruption == 'receipt': row.source_json = {}
    else:
        row.changes_json = [{'character': '陈川'}] if corruption == 'shape' else [dict(r.changes[0], evidence='正文没有的原句')]
        row.payload_hash = digest([r.source.source_hash, row.changes_json])
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
async def test_invalidated_source_is_excluded_even_if_its_old_payload_is_corrupt(cognition_db):
    from app.services.context_errors import RequiredContextUnavailable
    from app.services.entity_sources import resolve_entity_source
    r = cognition_db
    await run(r)
    r.session.execute(update(Record).values(payload_hash='broken'))
    r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(content_text='作者新稿。'))
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='not_ready'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)
    r.source = await resolve_entity_source(r.db, r.pid, 0, r.source.chapter_id)
    r.model.return_value = NS(text='[]', finish_reason='stop')
    await run(r)
    assert await history.load_ledger(r.db, r.pid, before_global_idx=901) == {}


@pytest.mark.asyncio
async def test_same_delivery_duplicate_cannot_reset_persisted_attempt_budget(cognition_db):
    r = cognition_db
    r.model.side_effect = RuntimeError('provider unavailable')
    for attempt in range(extraction.MAX_EXTRACTION_ATTEMPTS):
        with pytest.raises(RuntimeError): await run(r, recovery=True, delivery_id='original')
        assert stored(r).attempt_count == attempt + 1
    for _ in range(3):
        assert (await run(r, recovery=True, delivery_id='original'))['reason'] == 'model_retry_budget_exhausted'
    assert r.model.await_count == extraction.MAX_EXTRACTION_ATTEMPTS
    r.model.side_effect = None
    assert (await run(r, delivery_id='unrelated-new'))['status'] == 'skipped'
    assert stored(r).attempt_count == extraction.MAX_EXTRACTION_ATTEMPTS


@pytest.mark.asyncio
async def test_last_crashed_attempt_is_settled_without_more_model_calls(cognition_db):
    r = cognition_db
    await aged_record(r, 'running')
    r.session.execute(update(Record).values(attempt_count=extraction.MAX_EXTRACTION_ATTEMPTS, delivery_id='crashed'))
    r.session.commit()
    assert (await run(r, recovery=True, delivery_id='recovery'))['reason'] == 'model_retry_budget_exhausted'
    assert stored(r).status == 'failed'
    r.model.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_format_failure_cannot_overwrite_a_successor_or_start_repair(cognition_db):
    r = cognition_db
    winner = uuid4()
    async def model(**kwargs):
        r.session.execute(update(Record).values(attempt_token=winner, status='completed', changes_json=[],
            payload_hash=digest([r.source.source_hash, []])))
        r.session.commit()
        return NS(text='invalid json', finish_reason='stop')
    r.model.side_effect = model
    assert (await run(r))['status'] == 'superseded'
    assert stored(r).attempt_token == winner and stored(r).status == 'completed'
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_broker_failure_keeps_unacknowledged_work_recoverable(cognition_db):
    r = cognition_db
    await aged_record(r, 'pending')
    def broken(*args): raise RuntimeError('broker unavailable')
    assert (await extraction.replay_pending_cognition(r.sessions, dispatch=broken))['dispatched'] == 0
    assert stored(r).status == 'queued' and stored(r).attempt_count == 0
    await aged_record(r, 'queued')
    calls = []
    assert (await extraction.replay_pending_cognition(r.sessions, dispatch=lambda *a: calls.append(a)))['dispatched'] == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_registered_system_in_actual_prompt_runner_is_used_once(cognition_db, monkeypatch):
    from app.services import prompt_registry
    r = cognition_db
    registered = history.EXTRACTION_PROMPT + '\n作者定制：保留不可靠叙述的限定。'
    route = NS(system_prompt=registered, prompt_id=None)
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', actual_run_text_prompt)
    monkeypatch.setattr(prompt_registry, '_resolve_route_and_tier_cached', AsyncMock(return_value=(route, None)))
    async def generate(task, messages, **kwargs):
        assert task == 'chapter_cognition'
        assert messages[0] == {'role': 'system', 'content': registered}
        assert messages[1]['content'].endswith(r.source.content)
        assert not r.opened
        return NS(text='[]', finish_reason='stop', usage=NS(total_tokens=1))
    monkeypatch.setattr(prompt_registry, 'get_model_router_async', AsyncMock(return_value=NS(generate_with_tier_fallback=generate)))
    assert (await run(r))['changes'] == 0


@pytest.mark.asyncio
async def test_celery_entry_passes_original_request_id_and_has_finite_retry_policy(monkeypatch):
    from app.tasks import entity_tasks
    task = entity_tasks.extract_chapter_cognition
    execute = AsyncMock(return_value={'status': 'completed'})
    monkeypatch.setattr(entity_tasks, '_extract_cognition_async', execute)
    monkeypatch.setattr('app.tasks._run_async_safe', lambda coro: coro)
    task.push_request(id='broker-original')
    try:
        assert (await task.run('project', 'chapter', 'hash'))['status'] == 'completed'
    finally:
        task.pop_request()
    execute.assert_awaited_once_with('project', 'chapter', 'hash', delivery_id='broker-original', cycle_id=None)
    assert task.max_retries + 1 == extraction.MAX_EXTRACTION_ATTEMPTS


@pytest.mark.asyncio
async def test_delivery_migration_preserves_source_and_payload(cognition_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r = cognition_db
    await run(r)
    spec = importlib.util.spec_from_file_location('cognition_delivery_migration',
        Path(__file__).parents[1] / 'alembic/versions/a1001946_cognition_delivery.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    r.session.commit()
    with r.session.get_bind().begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        migration.upgrade()
    row = stored(r)
    assert row.source_hash == r.source.source_hash and row.changes_json == r.changes
    assert row.payload_hash == digest([r.source.source_hash, r.changes])
    assert row.status == 'completed' and row.delivery_id is None and row.attempt_count == 0


@pytest.mark.asyncio
async def test_shared_prompt_loader_uses_current_prose_and_retains_temporal_quotes(cognition_db):
    r = cognition_db
    await run(r)
    later = await add_state(r, 901, {}, body='沈青回来见陈川。')
    rendered = await history.load_prompt_context(r.db, r.pid, before_chapter_id=later.chapter_id,
        chapter_outline={'characters': ['陈川']}, chapter_text=later.content)
    assert r.changes[0]['evidence'] in rendered and '[CH-900]' in rendered
    assert '[CH-901]' not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'queued', 'running', 'failed', 'obsolete'])
async def test_known_incomplete_prior_source_cannot_become_empty_knowledge(cognition_db, status):
    from app.services.context_errors import RequiredContextUnavailable
    r = cognition_db
    await aged_record(r, status)
    with pytest.raises(RequiredContextUnavailable, match='not_ready'):
        await history.load_prompt_context(r.db, r.pid, before_global_idx=901, chapter_text='陈川回来。')


@pytest.mark.asyncio
async def test_cancelled_extraction_is_failed_and_old_delivery_cannot_revive_it(cognition_db):
    import asyncio
    r = cognition_db
    started = asyncio.Event()
    async def model(**kwargs):
        started.set()
        await asyncio.Event().wait()
    r.model.side_effect = model
    task = asyncio.create_task(run(r, delivery_id='cancelled'))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stored(r).status == 'failed' and stored(r).attempt_count == 1
    assert (await run(r, recovery=True, delivery_id='old-periodic'))['reason'] == 'model_retry_requires_original_task'
    r.model.assert_awaited_once()


@pytest.mark.asyncio
async def test_author_edit_requires_current_source_extraction(cognition_db):
    from app.services.context_errors import RequiredContextUnavailable
    r = cognition_db
    await aged_record(r, 'failed')
    r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(content_text='作者改写后的正文。'))
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='not_ready'):
        await history.load_ledger(r.db, r.pid, before_global_idx=901)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['missing', 'duplicate', 'zero', 'unspecified'])
async def test_shared_prompt_loader_requires_unambiguous_target_boundary(cognition_db, boundary):
    from app.services.context_errors import RequiredContextUnavailable
    r = cognition_db
    options = {}
    if boundary == 'missing': options['before_chapter_id'] = uuid4()
    elif boundary == 'duplicate':
        r.session.add(Chapter(volume_id=r.vid, chapter_idx=2, global_idx=900,
                              title='重复全书章号', content_text='重复的全书章号。', status='completed'))
        r.session.commit()
        options['before_chapter_id'] = r.source.chapter_id
    elif boundary == 'zero': options['before_global_idx'] = 0
    with pytest.raises(RequiredContextUnavailable, match='boundary'):
        await history.load_prompt_context(r.db, r.pid, **options)


@pytest.mark.asyncio
async def test_provider_failure_after_source_deletion_is_obsolete(cognition_db):
    r = cognition_db
    async def model(**kwargs):
        r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
        r.session.commit()
        raise RuntimeError('failed old request')
    r.model.side_effect = model
    assert (await run(r))['status'] == 'obsolete'
    r.model.assert_awaited_once()
    assert stored(r).status == 'obsolete'


@pytest.mark.asyncio
async def test_lease_lost_at_claim_commit_does_not_start_paid_model_call(cognition_db):
    r = cognition_db
    original = r.sessions
    commits = 0
    @asynccontextmanager
    async def sessions():
        nonlocal commits
        async with original() as db:
            commit = db.commit
            async def checked_commit():
                nonlocal commits
                await commit()
                commits += 1
                if commits == 2:
                    r.lease.check.side_effect = RuntimeError('lease lost at claim commit')
            db.commit = checked_commit
            yield db
    r.sessions = sessions
    with pytest.raises(RuntimeError, match='lease lost'):
        await run(r)
    r.model.assert_not_awaited()
    assert stored(r).status == 'failed' and stored(r).attempt_count == 1
