"""Bounded, source-checked cleanup of derived cognition indexes.

All index users share a dedicated project mutex. Only recognized version-one
points may be removed from live books, and a complete current ledger must be
available. Unknown payloads and reversible trash remain untouched.
"""
import asyncio
from dataclasses import dataclass
import logging
import re
from uuid import UUID, NAMESPACE_URL, uuid5

from qdrant_client.models import Distance, PointIdsList, UpdateStatus, VectorParams
from sqlalchemy import func, select

from app.models.project import Chapter, Project, Volume
from app.services.cognition_index_lock import cognition_index_lock
from app.services.context_errors import RequiredContextUnavailable
from app.services.embedding_space import current_embedding_space
from app.services.qdrant_store import COGNITION_PREFIX

logger = logging.getLogger(__name__)
_NAME = re.compile(rf'{COGNITION_PREFIX}__([0-9a-f]{{32}})__([0-9a-f]{{64}})__([1-9][0-9]*)')
_HASH = re.compile('[0-9a-f]{64}')
PAGE_SIZE = 256
SCAN_PAGES = 64
CLEANUP_SECONDS = 120


@dataclass(frozen=True)
class IndexName:
    name: str
    project_id: str
    space_id: str
    dimension: int


def parse_index_name(name):
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    return IndexName(name, str(UUID(hex=match[1])), match[2], int(match[3])) if match else None


def indexed_project(name):
    parsed = parse_index_name(name)
    return parsed.project_id if parsed else None


def owned_point(point, index):
    """Names alone do not authorize deletion of unknown/future payloads."""
    payload = point.payload
    if not isinstance(payload, dict) or set(payload) != {'version', 'project_id', 'space_id', 'content_hash', 'event_key'}:
        return False
    if (type(payload['version']) is not int or payload['version'] != 1
            or payload['project_id'] != index.project_id or payload['space_id'] != index.space_id
            or any(not isinstance(payload[key], str) or not _HASH.fullmatch(payload[key])
                   for key in ('content_hash', 'event_key'))):
        return False
    return str(point.id) == str(uuid5(NAMESPACE_URL, payload['content_hash']))


def scan_cursor(cursor, project_id):
    if cursor is None:
        return None, None
    if not isinstance(cursor, dict) or set(cursor) != {'collection', 'offset'}:
        raise ValueError('cognition_cleanup_cursor_invalid')
    parsed = parse_index_name(cursor['collection'])
    if parsed is None or parsed.project_id != project_id:
        raise ValueError('cognition_cleanup_cursor_invalid')
    offset = cursor['offset']
    if offset is not None:
        if type(offset) is int:
            if offset < 0:
                raise ValueError('cognition_cleanup_cursor_invalid')
        elif not isinstance(offset, str) or str(UUID(offset)) != offset:
            raise ValueError('cognition_cleanup_cursor_invalid')
    return parsed.name, offset


async def load_index_snapshot(sessions, project_id):
    """A full-book source witness, never an inference from missing index rows."""
    from app.services.character_cognition import load_ledger
    from app.services.cognition_retrieval import CognitionPrompt
    async with sessions() as db:
        project = (await db.execute(select(Project.id, Project.deleted_at)
            .where(Project.id == project_id).execution_options(autoflush=False))).one_or_none()
        if project is None or project.deleted_at is not None:
            return None
        maximum_query = select(func.max(Chapter.global_idx)).join(Volume, Chapter.volume_id == Volume.id).where(
            Volume.project_id == project_id).execution_options(autoflush=False)
        maximum = await db.scalar(maximum_query)
        boundary = max(maximum or 0, 0) + 1
        ledger = await load_ledger(db, project_id, before_global_idx=boundary)
        if await db.scalar(maximum_query) != maximum:
            raise RequiredContextUnavailable('character_cognition_boundary_unavailable')
    return CognitionPrompt.create(project_id, ledger, '', (), 0,
        before_global_idx=boundary, before_chapter_id=None)


async def configured_space_id(loader):
    space = await loader()
    try:
        identity = space.identity
        if not isinstance(identity, str) or not _HASH.fullmatch(identity):
            raise ValueError('cognition_cleanup_encoder_invalid')
        return identity
    finally:
        if hasattr(space, 'aclose'):
            async with asyncio.timeout(5):
                await space.aclose()


def new_client():
    from qdrant_client import AsyncQdrantClient
    from app.config import settings
    return AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT,
                            timeout=30, check_compatibility=False)


async def close_client(client):
    try:
        async with asyncio.timeout(5):
            await client.close()
    except Exception as exc:
        logger.warning('Cognition index cleanup close failed (%s)', type(exc).__name__)


def scoped_collection_names(project_id, names):
    if not isinstance(names, (list, tuple)):
        raise ValueError('cognition_cleanup_scope_invalid')
    for name in names:
        index = parse_index_name(name)
        if index is None or index.project_id != project_id:
            raise ValueError('cognition_cleanup_scope_invalid')
    return sorted(set(names))


async def project_indexes(client, project_id, names):
    if names is None:
        names = [item.name for item in (await client.get_collections()).collections
                 if indexed_project(item.name) == project_id]
    return [parse_index_name(name) for name in scoped_collection_names(project_id, names)]


async def remove_orphan_collections(project_id, names, sessions, client, lease):
    """Already owns this project's index lock; recheck absence before removal."""
    removed = 0
    for name in scoped_collection_names(project_id, names):
        async with sessions() as db:
            exists = await db.scalar(select(Project.id).where(Project.id == project_id))
        if exists is not None:
            break
        await lease.check()
        if await client.collection_exists(name):
            if not await client.delete_collection(collection_name=name):
                raise ValueError('cognition_cleanup_delete_unconfirmed')
            removed += 1
        await lease.check()
    return removed


async def cleanup_project_indexes(project_id, sessions, *, client=None, cursor=None,
                                  collection_names=None, space_loader=None, lock_factory=None, scan_pages=SCAN_PAGES):
    """Inspect bounded pages, recheck full sources, then delete exact owned IDs.

    The cursor is an optimization, not a source proof. Every continuation
    rebuilds its source witness. New/late points before the cursor are found by
    the next periodic sweep; no book-size cap or plaintext is stored in it.
    """
    from app.services.cognition_retrieval import iter_index_entries
    project_id = str(UUID(str(project_id)))
    start_name, start_offset = scan_cursor(cursor, project_id)
    if collection_names is not None:
        collection_names = scoped_collection_names(project_id, collection_names)
        if start_name is not None and start_name not in collection_names:
            raise ValueError('cognition_cleanup_cursor_invalid')
    if type(scan_pages) is not int or not 1 <= scan_pages <= SCAN_PAGES:
        raise ValueError('cognition_cleanup_page_limit_invalid')
    space_loader = space_loader or current_embedding_space
    lock_factory = lock_factory or cognition_index_lock
    owns_client = client is None
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            async with lock_factory(project_id, wait=False) as lease:
                if not lease:
                    return {'status': 'busy', 'cursor': cursor, 'removed_points': 0, 'removed_collections': 0}
                await lease.check()
                snapshot = await load_index_snapshot(sessions, project_id)
                if snapshot is None:
                    async with sessions() as db:
                        exists = await db.scalar(select(Project.id).where(Project.id == project_id))
                    if exists is not None:
                        return {'status': 'inactive', 'cursor': None, 'removed_points': 0, 'removed_collections': 0}
                    client = client or new_client()
                    indexes = await project_indexes(client, project_id, collection_names)
                    removed = await remove_orphan_collections(project_id, [index.name for index in indexes],
                        sessions, client, lease)
                    # Whole-collection deletion does not claim a point count.
                    return {'status': 'complete', 'cursor': None, 'removed_points': None, 'removed_collections': removed}
                space_id = await configured_space_id(space_loader)
                keep = {pid: entry['payload'] for pid, entry in iter_index_entries(snapshot, space_id)}
                client = client or new_client()
                indexes = [index for index in await project_indexes(client, project_id, collection_names)
                           if start_name is None or index.name >= start_name]
                pages = scanned = unknown = unmanaged = 0
                victims = {}
                complete_old = []
                next_cursor = None
                for number, index in enumerate(indexes):
                    if not await client.collection_exists(index.name):
                        continue  # The previous delivery may have removed it.
                    info = await client.get_collection(index.name)
                    config = info.config.params.vectors
                    if not isinstance(config, VectorParams) or config.size != index.dimension or config.distance != Distance.COSINE:
                        unmanaged += 1
                        continue
                    offset = start_offset if index.name == start_name else None
                    while True:
                        await lease.check()
                        points, next_offset = await client.scroll(collection_name=index.name, offset=offset,
                            limit=PAGE_SIZE, with_payload=True, with_vectors=False)
                        if next_offset is not None and (not points or next_offset == offset):
                            raise ValueError('cognition_cleanup_scan_incomplete')
                        pages += 1
                        scanned += len(points)
                        for point in points:
                            if not owned_point(point, index):
                                unknown += 1
                            elif index.space_id != space_id or keep.get(str(point.id)) != point.payload:
                                victims.setdefault(index.name, set()).add(str(point.id))
                        if next_offset is None:
                            if index.space_id != space_id:
                                complete_old.append(index.name)
                            if pages >= scan_pages and number + 1 < len(indexes):
                                next_cursor = {'collection': indexes[number + 1].name, 'offset': None}
                            break
                        offset = str(next_offset) if isinstance(next_offset, UUID) else next_offset
                        if pages >= scan_pages:
                            next_cursor = {'collection': index.name, 'offset': offset}
                            break
                    if pages >= scan_pages:
                        break
                current = await load_index_snapshot(sessions, project_id)
                if current is None or current.snapshot_hash != snapshot.snapshot_hash:
                    raise RequiredContextUnavailable('character_cognition_decision_changed')
                if await configured_space_id(space_loader) != space_id:
                    raise RequiredContextUnavailable('character_cognition_embedding_changed')
                removed = removed_collections = 0
                for name, ids in victims.items():
                    ids = sorted(ids)
                    for start in range(0, len(ids), PAGE_SIZE):
                        await lease.check()
                        batch = ids[start:start + PAGE_SIZE]
                        result = await client.delete(collection_name=name, points_selector=PointIdsList(points=batch), wait=True)
                        if result.status != UpdateStatus.COMPLETED:
                            raise ValueError('cognition_cleanup_delete_unconfirmed')
                        removed += len(batch)
                for name in complete_old:
                    await lease.check()
                    remaining = (await client.count(collection_name=name, exact=True)).count
                    if type(remaining) is not int or remaining < 0:
                        raise ValueError('cognition_cleanup_count_invalid')
                    if remaining == 0:
                        if not await client.delete_collection(collection_name=name):
                            raise ValueError('cognition_cleanup_delete_unconfirmed')
                        removed_collections += 1
                await lease.check()
                return {'status': 'partial' if next_cursor else 'complete', 'cursor': next_cursor,
                    'scanned_points': scanned, 'unknown_points': unknown, 'unmanaged_collections': unmanaged,
                    'removed_points': removed, 'removed_collections': removed_collections}
    finally:
        if owns_client and client is not None:
            await close_client(client)


async def cleanup_deleted_projects(sessions, *, client=None, lock_factory=None, collections=None):
    """DB lookup failure aborts deletion; never infer absence from an error."""
    owns_client = client is None
    lock_factory = lock_factory or cognition_index_lock
    deleted = busy = 0
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            client = client or new_client()
            collections = collections if collections is not None else (await client.get_collections()).collections
            names = [(item.name, indexed_project(item.name)) for item in collections]
            candidates = [(name, pid) for name, pid in names if pid is not None]
            for start in range(0, len(candidates), 200):
                batch = candidates[start:start + 200]
                async with sessions() as db:
                    live = {str(pid) for pid in (await db.execute(
                        select(Project.id).where(Project.id.in_([pid for _, pid in batch]))
                    )).scalars().all()}
                for name, pid in batch:
                    if pid not in live:
                        async with lock_factory(pid, wait=False) as lease:
                            if not lease:
                                busy += 1
                                continue
                            deleted += await remove_orphan_collections(pid, [name], sessions, client, lease)
            return {'deleted': deleted, 'examined': len(candidates), 'busy': busy}
    finally:
        if owns_client and client is not None:
            await close_client(client)


async def discover_index_cleanup(sessions, *, client=None):
    """Read directory once, excluding trash; deletion happens in per-book jobs."""
    owns_client = client is None
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            client = client or new_client()
            collections = (await client.get_collections()).collections
            grouped = {}
            for item in collections:
                pid = indexed_project(item.name)
                if pid is not None:
                    grouped.setdefault(pid, []).append(item.name)
            pids = sorted(grouped)
            live = {}
            for start in range(0, len(pids), 200):
                async with sessions() as db:
                    live.update({str(pid): deleted_at for pid, deleted_at in (await db.execute(
                        select(Project.id, Project.deleted_at).where(Project.id.in_(pids[start:start + 200]))
                        .execution_options(autoflush=False))).all()})
            return {'examined': sum(len(names) for names in grouped.values()),
                'projects': {pid: sorted(set(grouped[pid])) for pid in pids if pid not in live or live[pid] is None}}
    finally:
        if owns_client and client is not None:
            await close_client(client)
