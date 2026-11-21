"""Real local Qdrant retrieval with deterministic vectors, not a semantic eval."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from qdrant_client import AsyncQdrantClient

from app.services import cognition_retrieval as retrieval
from app.services.cognition_context import observation_key, render_history, history_fits
from app.services.cognition_validation import CognitionLedger
from app.services.context_errors import RequiredContextUnavailable
from app.services.context_pack import ContextPack
from app.services.entity_sources import digest
from app.services.qdrant_store import cognition_collection


def event(index, fact, *, kind='learns', evidence=None):
    return dict(chapter_idx=index, kind=kind, fact=fact, evidence=evidence or fact,
                source_hash=digest([index, fact]), source_chapter_id=str(uuid4()), source_mode='auto')


def prompt(ledger=None, *, project_id=None, context='村里的水路后来怎样了？', max_chars=600, priority_names=()):
    ledger = CognitionLedger(ledger or {f'路人{i}': {'history': [event(i + 1, f'支线消息{i}')]} for i in range(75)})
    ledger.snapshot_hash = digest(ledger)
    return retrieval.CognitionPrompt.create(project_id or uuid4(), ledger, context, priority_names, max_chars,
        before_global_idx=20000, before_chapter_id=None)


class FixedSpace:
    identity = digest('fixed-offline-encoder-v1')
    def __init__(self, hook=None): self.calls = []; self.hook = hook
    async def embed(self, texts, *, input_type):
        assert 1 <= len(texts) <= 32 and all(len(t) <= 4000 for t in texts)
        self.calls.append((input_type, list(texts)))
        if self.hook is not None: await self.hook(input_type, texts)
        if input_type == 'query': return [[1.0, 0.0] for _ in texts]
        return [[1.0, 0.0] if '旧井' in text else [0.1, 0.9] for text in texts]


@pytest_asyncio.fixture
async def vector_db(monkeypatch):
    # PostgreSQL mutex behavior is tested separately; local Qdrant tests must
    # never open a runtime service connection.
    @asynccontextmanager
    async def index_lock(project_id, *, wait=False):
        yield NS(check=AsyncMock())
    monkeypatch.setattr(retrieval, 'cognition_index_lock', index_lock)
    monkeypatch.setattr('app.services.cognition_index_maintenance.cognition_index_lock', index_lock)
    client = AsyncQdrantClient(':memory:')
    try: yield client
    finally: await client.close()


def sessions_for(ledger, monkeypatch):
    @asynccontextmanager
    async def sessions(): yield NS()
    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value=ledger))
    return sessions


@pytest.mark.asyncio
async def test_current_event_recall_keeps_later_knowledge_and_full_quotes(vector_db):
    old = event(1, '旧井不知为何被封。', kind='suspects', evidence='游商猜是井水出了问题。')
    latest = event(100, '封井是检修，原因已经查明。', evidence='游商看见告示，这才明白是检修。')
    ledger = {'游商': {'history': [old, latest]}}
    ledger.update({f'邻人{i}': {'history': [event(110 + i, f'另路消息{i}。')]} for i in range(75)})
    request = prompt(ledger)
    keys = await retrieval.rank_cognition(request, client=vector_db, space=FixedSpace())
    assert keys[0] == observation_key('游商', old)
    rendered = render_history(ledger, 600, ranked_keys=keys)
    assert old['evidence'] in rendered and latest['evidence'] in rendered
    assert '当时怀疑' in rendered and rendered.index('[CH-1]') < rendered.index('[CH-100]')


@pytest.mark.asyncio
async def test_a_long_multislice_quote_cannot_occupy_all_event_slots(vector_db):
    long = event(1, '旧井的多年沿革。', evidence='旧井水路沿革。' * 50000)
    other = event(2, '另一条有用的水路消息。')
    request = prompt({'访客': {'history': [long]}, '船夫': {'history': [other]}})
    entries = retrieval.index_entries(request, FixedSpace.identity)
    assert len(entries) > retrieval.RETRIEVAL_LIMIT
    space = FixedSpace()
    keys = await retrieval.rank_cognition(request, client=vector_db, space=space)
    assert set(keys) == {observation_key('访客', long), observation_key('船夫', other)}
    assert sum(len(texts) for kind, texts in space.calls if kind == 'passage') == len(entries)


@pytest.mark.asyncio
async def test_only_current_immutable_points_compete_and_warm_index_is_reused(vector_db):
    obsolete = prompt({f'旧人{i}': {'history': [event(i + 1, '旧井的过期消息。')]} for i in range(90)})
    await retrieval.rank_cognition(obsolete, client=vector_db, space=FixedSpace())
    current = prompt(project_id=obsolete.project_id)
    space = FixedSpace()
    keys = await retrieval.rank_cognition(current, client=vector_db, space=space)
    expected = {entry['key'] for entry in retrieval.index_entries(current, space.identity).values()}
    assert len(keys) == 64 and set(keys) <= expected
    assert sum(len(texts) for kind, texts in space.calls if kind == 'passage') == 75
    space.calls.clear()
    assert await retrieval.rank_cognition(current, client=vector_db, space=space) == keys
    assert [kind for kind, _ in space.calls] == ['query']
    # No quote, fact, or character label is stored in Qdrant payloads.
    points, _ = await vector_db.scroll(cognition_collection(current.project_id, space.identity, 2), limit=200)
    assert len(points) == 165
    assert all(set(p.payload) == {'version', 'project_id', 'space_id', 'content_hash', 'event_key'} for p in points)


@pytest.mark.asyncio
async def test_project_and_encoder_namespaces_do_not_share_points(vector_db):
    a = prompt(); b = replace(a, project_id=str(uuid4()))
    first = FixedSpace(); second = FixedSpace(); second.identity = digest('second-encoder')
    for request, space in ((a, first), (b, first), (a, second)):
        await retrieval.rank_cognition(request, client=vector_db, space=space)
    names = {c.name for c in (await vector_db.get_collections()).collections}
    assert names == {cognition_collection(p.project_id, s.identity, 2) for p, s in ((a, first), (b, first), (a, second))}


@pytest.mark.asyncio
async def test_cancelled_index_preparation_reuses_only_acknowledged_batches(vector_db, monkeypatch):
    request = prompt({f'路人{i}': {'history': [event(i + 1, f'消息{i}')]} for i in range(140)})
    first_written = asyncio.Event(); blocked = asyncio.Event(); calls = 0
    async def hook(kind, texts):
        nonlocal calls
        if kind == 'passage':
            calls += 1
            if calls > 1: await blocked.wait()
    upsert = vector_db.upsert
    async def write(**kwargs):
        result = await upsert(**kwargs)
        first_written.set()
        return result
    monkeypatch.setattr(vector_db, 'upsert', write)
    task = asyncio.create_task(retrieval.rank_cognition(request, client=vector_db, space=FixedSpace(hook)))
    await asyncio.wait_for(first_written.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    name = cognition_collection(request.project_id, FixedSpace.identity, 2)
    assert (await vector_db.count(name, exact=True)).count == 32
    resumed = FixedSpace()
    assert await retrieval.rank_cognition(request, client=vector_db, space=resumed)
    assert sum(len(texts) for kind, texts in resumed.calls if kind == 'passage') == 108
    assert (await vector_db.count(name, exact=True)).count == 140


@pytest.mark.asyncio
async def test_short_history_needs_no_embeddings_and_empty_history_fits_zero_budget():
    loader = AsyncMock(side_effect=AssertionError('must not load'))
    assert history_fits({}, 0)
    assert await retrieval.resolve_prompt_context('完整短历史', space_loader=loader) == '完整短历史'
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_borrowed_pending_transaction_is_not_committed_or_rolled_back():
    db = NS(in_transaction=lambda: True, commit=AsyncMock(), rollback=AsyncMock())
    loader = AsyncMock()
    with pytest.raises(RequiredContextUnavailable, match='transaction_open'):
        await retrieval.resolve_prompt_context(prompt(), db=db, space_loader=loader)
    loader.assert_not_awaited(); db.commit.assert_not_awaited(); db.rollback.assert_not_awaited()


def test_unresolved_prompt_cannot_be_printed_or_silently_rendered():
    request = prompt()
    with pytest.raises(RequiredContextUnavailable, match='pending'): str(request)
    with pytest.raises(RequiredContextUnavailable, match='pending'): ContextPack(cognition_boundaries=request).to_system_prompt()


@pytest.mark.asyncio
async def test_configuration_change_during_recall_requires_fresh_context(vector_db, monkeypatch):
    request = prompt(); first = FixedSpace(); changed = FixedSpace(); changed.identity = digest('changed')
    loader = AsyncMock(side_effect=[first, changed])
    with pytest.raises(RequiredContextUnavailable, match='embedding_changed'):
        await retrieval.resolve_prompt_context(request, client=vector_db, space_loader=loader,
            sessions=sessions_for(request.ledger, monkeypatch))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['embedding', 'search', 'dimension', 'empty', 'receipt'])
async def test_invalid_retrieval_never_becomes_an_empty_success(vector_db, monkeypatch, failure):
    request = prompt(); space = FixedSpace()
    if failure == 'embedding': space.embed = AsyncMock(side_effect=RuntimeError('private upstream detail'))
    if failure == 'dimension':
        async def wrong(texts, *, input_type): return [[1, 0] if input_type == 'query' else [1, 0, 0] for _ in texts]
        space.embed = wrong
    if failure == 'search': monkeypatch.setattr(vector_db, 'query_points_groups', AsyncMock(side_effect=RuntimeError('offline failure')))
    if failure == 'empty': monkeypatch.setattr(vector_db, 'query_points_groups', AsyncMock(return_value=NS(groups=[])))
    if failure == 'receipt':
        monkeypatch.setattr(vector_db, 'query_points_groups', AsyncMock(return_value=NS(groups=[NS(id='wrong', hits=[NS(id=str(uuid4()), payload={}, score=1)])])))
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable') as error:
        await retrieval.resolve_prompt_context(request, client=vector_db, space_loader=AsyncMock(return_value=space),
            sessions=sessions_for(request.ledger, monkeypatch))
    assert 'private' not in str(error.value)


@pytest.mark.asyncio
async def test_owned_resources_close_after_success_without_masking_result(vector_db, monkeypatch):
    request = prompt(); space = FixedSpace(); space.aclose = AsyncMock(side_effect=RuntimeError('close failed'))
    client = NS(close=AsyncMock(side_effect=RuntimeError('close failed')))
    monkeypatch.setattr('qdrant_client.AsyncQdrantClient', lambda **kwargs: client)
    monkeypatch.setattr(retrieval, 'rank_cognition', AsyncMock(return_value=[]))
    result = await retrieval.resolve_prompt_context(request, space_loader=AsyncMock(return_value=space),
        sessions=sessions_for(request.ledger, monkeypatch))
    assert isinstance(result, str) and '认知记录' in result
    client.close.assert_awaited_once(); space.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_total_deadline_and_cancellation_release_owned_provider(vector_db, monkeypatch, cancel):
    request = prompt(); started = asyncio.Event()
    async def blocked(kind, texts):
        started.set()
        await asyncio.Event().wait()
    space = FixedSpace(blocked); space.aclose = AsyncMock()
    monkeypatch.setattr(retrieval, 'RETRIEVAL_SECONDS', 30 if cancel else 0.03)
    task = asyncio.create_task(retrieval.resolve_prompt_context(request, client=vector_db,
        space_loader=AsyncMock(return_value=space), sessions=sessions_for(request.ledger, monkeypatch)))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'): await task
    space.aclose.assert_awaited_once()
