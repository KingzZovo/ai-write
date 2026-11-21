"""Live-book cleanup against actual SQLite sources and in-memory Qdrant."""
import json
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import delete, update

from app.models.cognition import ChapterCognition as Record
from app.models.project import Chapter, Project
from app.services import cognition_index_maintenance as cleanup
from app.services.cognition_retrieval import index_entries
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest
from app.services.qdrant_store import cognition_collection
from tests.test_cognition_author_edits import decide
from tests.test_cognition_retrieval import FixedSpace, vector_db
from tests.test_cognition_validation import hashed_db, cognition_db, state_db, run


async def seed(client, snapshot, space_id):
    entries = index_entries(snapshot, space_id)
    name = cognition_collection(snapshot.project_id, space_id, 2)
    if not await client.collection_exists(name):
        await client.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    if entries:
        await client.upsert(name, [PointStruct(id=pid, vector=[1.0, 0.1], payload=entry['payload'])
            for pid, entry in entries.items()], wait=True)
    return name, entries


async def current_snapshot(r):
    return await cleanup.load_index_snapshot(r.sessions, str(r.pid))


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown_old', [False, True])
async def test_author_changes_and_encoder_switch_remove_only_owned_obsolete_points(hashed_db, vector_db, unknown_old):
    r = hashed_db
    await run(r)
    old = await current_snapshot(r)
    before_body = r.source.content
    old_name, old_entries = await seed(vector_db, old, digest('old-encoder'))
    current_name, obsolete = await seed(vector_db, old, FixedSpace.identity)
    await decide(r, [dict(character='陈川', suspects='父亲可能已收到信', evidence=r.changes[0]['evidence'])])
    current = await current_snapshot(r)
    _, keep = await seed(vector_db, current, FixedSpace.identity)
    other = replace(current, project_id=str(uuid4()))
    other_name, other_entries = await seed(vector_db, other, FixedSpace.identity)
    unknown = str(uuid4())
    if unknown_old:
        await vector_db.upsert(old_name, [PointStruct(id=unknown, vector=[1.0, 0.1], payload={'version': 2})])
    provider = FixedSpace()
    result = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        space_loader=AsyncMock(return_value=provider))
    assert result['status'] == 'complete'
    assert result['removed_points'] == len(old_entries) + len(obsolete)
    assert result['removed_collections'] == (0 if unknown_old else 1)
    assert result['unknown_points'] == int(unknown_old)
    points, _ = await vector_db.scroll(current_name, limit=100)
    assert {str(point.id) for point in points} == set(keep)
    points, _ = await vector_db.scroll(other_name, limit=100)
    assert {str(point.id) for point in points} == set(other_entries)
    assert (await vector_db.collection_exists(old_name)) == unknown_old
    assert not provider.calls  # Cleanup never invokes embeddings or fiction models.
    assert r.session.get(Chapter, UUID(r.source.chapter_id)).content_text == before_body
    again = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        space_loader=AsyncMock(return_value=provider))
    assert again['removed_points'] == 0 and again['removed_collections'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'pending', 'body', 'position', 'configuration'])
async def test_incomplete_sources_or_unknown_config_never_authorize_deletion(hashed_db, vector_db, monkeypatch, failure):
    r = hashed_db
    await run(r)
    snapshot = await current_snapshot(r)
    await seed(vector_db, snapshot, FixedSpace.identity)
    if failure == 'missing':
        r.session.execute(delete(Record)); r.session.commit()
    elif failure == 'pending':
        r.session.execute(update(Record).values(status='pending')); r.session.commit()
    elif failure == 'body':
        r.session.execute(update(Chapter).values(content_text='作者改过稿，旧提取无效。')); r.session.commit()
    elif failure == 'position':
        r.session.execute(update(Chapter).values(global_idx=None)); r.session.commit()
    remove = AsyncMock()
    monkeypatch.setattr(vector_db, 'delete', remove)
    loader = AsyncMock(side_effect=ValueError('not configured')) if failure == 'configuration' else AsyncMock(return_value=FixedSpace())
    with pytest.raises((RequiredContextUnavailable, ValueError)):
        await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db, space_loader=loader)
    remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['author', 'body', 'trash', 'encoder', 'delete'])
async def test_source_changes_during_scan_cancel_all_planned_deletion(hashed_db, vector_db, monkeypatch, mutation):
    r = hashed_db
    await run(r)
    await seed(vector_db, await current_snapshot(r), digest('old-encoder'))
    scroll = vector_db.scroll
    changed = False
    async def mutate(**kwargs):
        nonlocal changed
        assert not r.opened
        result = await scroll(**kwargs)
        if not changed:
            changed = True
            if mutation == 'author':
                await decide(r, [])
            elif mutation == 'body':
                r.session.execute(update(Chapter).values(content_text='已改稿。')); r.session.commit()
            elif mutation == 'trash':
                r.session.execute(update(Project).values(deleted_at=datetime.now(timezone.utc))); r.session.commit()
            elif mutation == 'delete':
                r.session.execute(delete(Chapter)); r.session.commit()
        return result
    monkeypatch.setattr(vector_db, 'scroll', mutate)
    remove = AsyncMock()
    monkeypatch.setattr(vector_db, 'delete', remove)
    first, last = FixedSpace(), FixedSpace()
    if mutation == 'encoder':
        last.identity = digest('newer-encoder')
    with pytest.raises(RequiredContextUnavailable):
        await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
            space_loader=AsyncMock(side_effect=[first, last]))
    assert changed
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_empty_author_decision_removes_derived_points_but_keeps_source(hashed_db, vector_db):
    r = hashed_db
    await run(r)
    name, entries = await seed(vector_db, await current_snapshot(r), FixedSpace.identity)
    await decide(r, [])
    result = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        space_loader=AsyncMock(return_value=FixedSpace()))
    assert result['removed_points'] == len(entries)
    assert (await vector_db.count(name, exact=True)).count == 0
    assert r.session.get(Chapter, UUID(r.source.chapter_id)).content_text == r.source.content


@pytest.mark.asyncio
async def test_cursor_progresses_past_live_points_and_keeps_unknown_payloads(hashed_db, vector_db, monkeypatch):
    r = hashed_db
    changes = r.changes + [dict(character=f'路人{i}', learns=f'不同的消息{i}', evidence=r.changes[0]['evidence']) for i in range(20)]
    r.model.return_value = NS(text=json.dumps(changes, ensure_ascii=False), finish_reason='stop')
    await run(r)
    original = await current_snapshot(r)
    name, obsolete = await seed(vector_db, original, FixedSpace.identity)
    await decide(r, changes[:10])
    _, keep = await seed(vector_db, await current_snapshot(r), FixedSpace.identity)
    unknown = str(uuid4())
    await vector_db.upsert(name, [PointStruct(id=unknown, vector=[1.0, 0.1], payload={'version': 99})])
    monkeypatch.setattr(cleanup, 'PAGE_SIZE', 4)
    cursor = None
    removed = scans = 0
    for _ in range(30):
        result = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db, cursor=cursor,
            space_loader=AsyncMock(return_value=FixedSpace()), scan_pages=1)
        removed += result['removed_points']; scans += 1
        assert result['scanned_points'] <= 4
        cursor = result['cursor']
        if cursor is None:
            assert result['status'] == 'complete'
            break
    else:
        pytest.fail('cursor never completed the book')
    assert scans > 2 and removed == len(obsolete)
    points, _ = await vector_db.scroll(name, limit=100)
    assert {str(point.id) for point in points} == set(keep) | {unknown}


@pytest.mark.asyncio
async def test_discovery_deduplicates_live_books_and_excludes_trash(hashed_db, vector_db):
    r = hashed_db
    await run(r)
    snapshot = await current_snapshot(r)
    first, _ = await seed(vector_db, snapshot, FixedSpace.identity)
    second, _ = await seed(vector_db, snapshot, digest('older-space'))
    result = await cleanup.discover_index_cleanup(r.sessions, client=vector_db)
    assert result['projects'] == {str(r.pid): sorted([first, second])}
    r.session.execute(update(Project).values(deleted_at=datetime.now(timezone.utc))); r.session.commit()
    result = await cleanup.discover_index_cleanup(r.sessions, client=vector_db)
    assert result['projects'] == {}
    assert (await vector_db.get_collections()).collections


@pytest.mark.asyncio
async def test_discovery_only_reads_directory_and_sends_orphans_to_individual_jobs(hashed_db, vector_db, monkeypatch):
    r = hashed_db
    await run(r)
    snapshot = await current_snapshot(r)
    active, _ = await seed(vector_db, snapshot, FixedSpace.identity)
    orphan = replace(snapshot, project_id=str(uuid4()))
    orphan_name, _ = await seed(vector_db, orphan, FixedSpace.identity)
    drop = AsyncMock(return_value=True)
    monkeypatch.setattr(vector_db, 'delete_collection', drop)
    result = await cleanup.discover_index_cleanup(r.sessions, client=vector_db)
    drop.assert_not_awaited()
    assert result['projects'] == {str(r.pid): [active], orphan.project_id: [orphan_name]}


@pytest.mark.asyncio
async def test_worker_uses_scoped_directory_without_global_rescan(hashed_db, vector_db, monkeypatch):
    r = hashed_db
    await run(r)
    snapshot = await current_snapshot(r)
    active, keep = await seed(vector_db, snapshot, FixedSpace.identity)
    old, stale = await seed(vector_db, snapshot, digest('old-directory-model'))
    orphan = replace(snapshot, project_id=str(uuid4()))
    orphan_name, _ = await seed(vector_db, orphan, FixedSpace.identity)
    global_scan = AsyncMock(side_effect=AssertionError('workers must not list all projects'))
    monkeypatch.setattr(vector_db, 'get_collections', global_scan)
    result = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        collection_names=[active, old], space_loader=AsyncMock(return_value=FixedSpace()))
    assert result['removed_points'] == len(stale)
    assert (await vector_db.count(active, exact=True)).count == len(keep)
    orphan_result = await cleanup.cleanup_project_indexes(orphan.project_id, r.sessions, client=vector_db,
        collection_names=[orphan_name], space_loader=AsyncMock(side_effect=AssertionError('no config needed for orphan')))
    assert orphan_result['status'] == 'complete' and orphan_result['removed_collections'] == 1
    global_scan.assert_not_awaited()
    # Redelivered jobs may contain a collection already acknowledged deleted.
    repeated = await cleanup.cleanup_project_indexes(orphan.project_id, r.sessions, client=vector_db,
        collection_names=[orphan_name])
    assert repeated['removed_collections'] == 0
    with pytest.raises(ValueError):
        await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
            collection_names=[orphan_name])


@pytest.mark.asyncio
async def test_cancelled_delete_can_resume_without_losing_current_points(hashed_db, vector_db, monkeypatch):
    r = hashed_db
    changes = r.changes + [dict(character=f'路人{i}', learns=f'另一个消息{i}', evidence=r.changes[0]['evidence']) for i in range(8)]
    r.model.return_value = NS(text=json.dumps(changes, ensure_ascii=False), finish_reason='stop')
    await run(r)
    snapshot = await current_snapshot(r)
    old_name, obsolete = await seed(vector_db, snapshot, digest('old-cancelled'))
    name, keep = await seed(vector_db, snapshot, FixedSpace.identity)
    monkeypatch.setattr(cleanup, 'PAGE_SIZE', 2)
    remove = vector_db.delete
    calls = 0
    async def interrupted(**kwargs):
        nonlocal calls
        assert not r.opened
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
        return await remove(**kwargs)
    monkeypatch.setattr(vector_db, 'delete', interrupted)
    with pytest.raises(asyncio.CancelledError):
        await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
            space_loader=AsyncMock(return_value=FixedSpace()))
    assert (await vector_db.count(old_name, exact=True)).count == len(obsolete) - 2
    assert (await vector_db.count(name, exact=True)).count == len(keep)
    monkeypatch.setattr(vector_db, 'delete', remove)
    resumed = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        space_loader=AsyncMock(return_value=FixedSpace()))
    assert resumed['removed_points'] == len(obsolete) - 2 and resumed['status'] == 'complete'
    assert not await vector_db.collection_exists(old_name)
    assert (await vector_db.count(name, exact=True)).count == len(keep)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['acknowledged', 'invalid_count', 'unmanaged_config'])
async def test_unknown_configuration_and_unconfirmed_deletes_do_not_claim_success(hashed_db, vector_db, monkeypatch, fault):
    r = hashed_db
    await run(r)
    name, _ = await seed(vector_db, await current_snapshot(r), digest('old-encoder'))
    drop = AsyncMock()
    monkeypatch.setattr(vector_db, 'delete_collection', drop)
    if fault == 'acknowledged':
        monkeypatch.setattr(vector_db, 'delete', AsyncMock(return_value=NS(status='acknowledged')))
    elif fault == 'invalid_count':
        monkeypatch.setattr(vector_db, 'count', AsyncMock(return_value=NS(count=False)))
    else:
        monkeypatch.setattr(vector_db, 'get_collection', AsyncMock(return_value=NS(
            config=NS(params=NS(vectors=VectorParams(size=3, distance=Distance.COSINE))))))
    if fault == 'unmanaged_config':
        result = await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
            space_loader=AsyncMock(return_value=FixedSpace()))
        assert result['unmanaged_collections'] == 1 and result['removed_points'] == 0
    else:
        with pytest.raises(ValueError):
            await cleanup.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
                space_loader=AsyncMock(return_value=FixedSpace()))
    drop.assert_not_awaited()


@pytest.mark.parametrize('offset', [True, -1, 'not-a-uuid', [], {}])
def test_invalid_cursor_cannot_be_used_as_deletion_scope(offset):
    pid = str(uuid4())
    with pytest.raises(ValueError):
        cleanup.scan_cursor({'collection': cognition_collection(pid, FixedSpace.identity, 2), 'offset': offset}, pid)


@pytest.mark.parametrize('mutation', ['foreign_project', 'foreign_space', 'future_version', 'bool_version',
    'extra_field', 'missing_field', 'bad_hash', 'bad_id'])
def test_payload_ownership_requires_exact_version_and_identity(mutation):
    from tests.test_cognition_retrieval import prompt
    snapshot = prompt()
    name = cognition_collection(snapshot.project_id, FixedSpace.identity, 2)
    pid, entry = next(iter(index_entries(snapshot, FixedSpace.identity).items()))
    payload = dict(entry['payload'])
    if mutation == 'foreign_project': payload['project_id'] = str(uuid4())
    elif mutation == 'foreign_space': payload['space_id'] = digest('other')
    elif mutation == 'future_version': payload['version'] = 2
    elif mutation == 'bool_version': payload['version'] = True
    elif mutation == 'extra_field': payload['unknown'] = 'future'
    elif mutation == 'missing_field': del payload['event_key']
    elif mutation == 'bad_hash': payload['event_key'] = 'bad'
    else: pid = str(uuid4())
    assert not cleanup.owned_point(NS(id=pid, payload=payload), cleanup.parse_index_name(name))
