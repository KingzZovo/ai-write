"""Exact cognition proofs on real SQLite SQL; no model or service connections.

SQLite triggers emulate the stored-hash invariant for these read-path tests.
The separate PostgreSQL migration tests verify the actual production trigger.
"""
import hashlib
import json
from time import perf_counter
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select, text, update

from app.models.cognition import ChapterCognition as Record, CognitionRevision as Revision
from app.models.project import Chapter, Project, Volume
from app.services import character_cognition as history, cognition_reader as reader
from app.services import cognition_validation as validation, entity_sources
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, EntitySourceHeader, resolve_entity_source
from tests.test_cognition_workflow_offline import cognition_db, state_db, run, stored, add_state
from tests.test_cognition_author_edits import decide


@pytest_asyncio.fixture
async def hashed_db(cognition_db, monkeypatch):
    r = cognition_db
    raw = r.session.connection().connection.driver_connection
    raw.create_function('sha256_utf8', 1, lambda value: hashlib.sha256((value or '').encode()).hexdigest())
    r.session.execute(text('''CREATE TRIGGER saved_hash_insert AFTER INSERT ON chapters BEGIN
        UPDATE chapters SET content_hash = sha256_utf8(NEW.content_text) WHERE id = NEW.id;
    END'''))
    r.session.execute(text('''CREATE TRIGGER saved_hash_update AFTER UPDATE OF content_text, content_hash ON chapters
        WHEN NEW.content_hash IS NOT sha256_utf8(NEW.content_text) BEGIN
        UPDATE chapters SET content_hash = sha256_utf8(NEW.content_text) WHERE id = NEW.id;
    END'''))
    r.session.execute(text('UPDATE chapters SET content_hash = sha256_utf8(content_text)'))
    r.session.commit()
    cache = validation.EvidenceProofCache()
    monkeypatch.setattr(validation, 'evidence_proofs', cache)
    r.cache = cache
    r.body_reads = []
    real = r.db.execute

    class MeasuredResult:
        def __init__(self, result): self.result = result
        def all(self):
            rows = self.result.all()
            r.body_reads.append(dict(rows=len(rows), chars=sum(len(row.content_text or '') for row in rows)))
            return rows

    async def measured(statement):
        result = await real(statement)
        if any(getattr(col, 'key', None) == 'content_text' for col in getattr(statement, 'selected_columns', [])):
            return MeasuredResult(result)
        return result

    r.db.execute = measured
    return r


async def load(r, *, before=901, **kwargs):
    return await history.load_ledger(r.db, r.pid, before_global_idx=before, **kwargs)


def save_payload(r, source, changes):
    r.session.execute(insert(Record).values(id=uuid4(), project_id=r.pid, chapter_id=UUID(source.chapter_id),
        source_hash=source.source_hash, source_json=source.receipt, status='completed',
        changes_json=changes, payload_hash=digest([source.source_hash, changes])))
    r.session.commit()


@pytest.mark.asyncio
async def test_header_matches_full_source_without_loading_prose(hashed_db):
    r = hashed_db
    headers = await entity_sources.resolve_entity_source_headers(r.db, r.pid, [r.source.chapter_id])
    header = headers[r.source.chapter_id]
    assert isinstance(header, EntitySourceHeader)
    assert header.receipt == r.source.receipt and header.source_hash == r.source.source_hash
    assert not r.body_reads
    assert await entity_sources.resolve_entity_source_headers(r.db, uuid4(), [r.source.chapter_id]) == {}


@pytest.mark.asyncio
async def test_cold_reads_body_once_and_warm_keeps_identical_full_history(hashed_db):
    r = hashed_db
    await run(r)
    r.body_reads.clear()
    cold = await load(r)
    assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]
    r.body_reads.clear()
    warm = await load(r)
    assert cold == warm and warm['陈川']['history'][0]['evidence'] == r.changes[0]['evidence']
    assert r.body_reads == []


@pytest.mark.asyncio
async def test_unknown_hash_still_uses_complete_sources(cognition_db):
    r = cognition_db
    await run(r)
    assert (await entity_sources.resolve_entity_source_headers(r.db, r.pid, [r.source.chapter_id]))[r.source.chapter_id] == r.source
    first = await load(r)
    r.session.execute(update(Chapter).values(content_text='作者改稿后没有这些原句。')); r.session.commit()
    with pytest.raises(RequiredContextUnavailable): await load(r)
    assert first['陈川']['history'][0]['evidence'] == r.changes[0]['evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing', 'pending', 'running', 'failed', 'obsolete', 'digest', 'receipt', 'shape', 'quote'])
async def test_warm_proof_cannot_mask_missing_failed_or_corrupt_payload(hashed_db, corruption):
    r = hashed_db
    await run(r)
    await load(r)
    row = stored(r)
    if corruption == 'missing': r.session.delete(row)
    elif corruption in {'pending', 'running', 'failed', 'obsolete'}: row.status = corruption
    elif corruption == 'digest': row.payload_hash = '0' * 64
    elif corruption == 'receipt': row.source_json = {}
    else:
        row.changes_json = [{'character': '陈川'}] if corruption == 'shape' else [dict(r.changes[0], evidence='没有写过这句')]
        row.payload_hash = digest([r.source.source_hash, row.changes_json])
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable): await load(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['', None, ' \t\r\n', '\u3000\u2003\u0085'])
async def test_completed_blank_body_with_valid_empty_receipt_is_not_certified(hashed_db, body):
    r = hashed_db
    r.session.execute(update(Chapter).values(content_text=body)); r.session.commit()
    header = (await entity_sources.resolve_entity_source_headers(r.db, r.pid, [r.source.chapter_id]))[r.source.chapter_id]
    save_payload(r, header, [])
    for _ in range(2):
        with pytest.raises(RequiredContextUnavailable, match='source_unavailable'): await load(r)


@pytest.mark.asyncio
async def test_valid_empty_result_has_proof_but_does_not_hide_a_body_edit(hashed_db):
    r = hashed_db
    r.model.return_value = NS(text='[]', finish_reason='stop')
    await run(r)
    assert await load(r) == {}
    r.body_reads.clear()
    assert await load(r) == {} and not r.body_reads
    r.session.execute(update(Chapter).values(content_text='陈川现在听到了消息。')); r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='not_ready'): await load(r)


@pytest.mark.asyncio
async def test_author_override_empty_and_restore_invalidate_model_proofs(hashed_db):
    r = hashed_db
    await run(r)
    model = await load(r)
    changes = [dict(character='陈川', suspects='他怀疑父亲已经收信', evidence=r.changes[0]['evidence'])]
    # An author interpretation is retained even when the automatic one differs.
    await decide(r, changes)
    corrected = await load(r)
    assert corrected['陈川']['history'][0]['kind'] == 'suspects'
    assert corrected['陈川']['history'][0]['source_mode'] == 'author'
    r.body_reads.clear()
    assert await load(r) == corrected and not r.body_reads
    await decide(r, [])
    assert await load(r) == {}
    await decide(r, mode='auto')
    assert await load(r) == model


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing', 'digest', 'source', 'quote'])
async def test_warm_author_proof_cannot_mask_bad_revision(hashed_db, corruption):
    r = hashed_db
    accepted = await decide(r, r.changes)
    await load(r)
    revision = r.session.get(Revision, UUID(accepted['revision_id']))
    if corruption == 'missing': r.session.delete(revision)
    elif corruption == 'digest': revision.payload_hash = 'bad'
    else:
        if corruption == 'source': revision.source_json = {}
        else: revision.changes_json = [dict(r.changes[0], evidence='无关且不存在的引文')]
        revision.payload_hash = digest(reader.revision_payload(revision))
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable): await load(r)


@pytest.mark.asyncio
async def test_raw_author_change_during_recheck_requires_rebuild_even_without_version_change(hashed_db, monkeypatch):
    r = hashed_db
    accepted = await decide(r, r.changes)
    await load(r)
    actual = reader.load_revisions
    calls = 0
    async def changed(db, pid, records, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            revision = r.session.get(Revision, UUID(accepted['revision_id']))
            revision.changes_json = []
            revision.payload_hash = digest(reader.revision_payload(revision))
            r.session.commit()
        return await actual(db, pid, records)
    monkeypatch.setattr(reader, 'load_revisions', changed)
    with pytest.raises(RequiredContextUnavailable, match='decision_changed'): await load(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['body', 'title', 'status', 'delete', 'chapter_order', 'volume_order', 'project', 'target'])
async def test_hot_read_rechecks_every_source_and_target(hashed_db, monkeypatch, mutation):
    r = hashed_db
    await run(r)
    target = await add_state(r, 901, {}, body='沈青回来见陈川。')
    await load(r)
    actual = entity_sources.resolve_entity_source_headers
    changed = False
    async def mutate_after_header(db, pid, ids):
        nonlocal changed
        headers = await actual(db, pid, ids)
        if not changed:
            changed = True
            if mutation == 'body': r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(content_text='全新保存稿。'))
            elif mutation == 'title': r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(title='新章名'))
            elif mutation == 'status': r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(status='draft'))
            elif mutation == 'delete': r.session.execute(delete(Chapter).where(Chapter.id == UUID(r.source.chapter_id)))
            elif mutation == 'chapter_order': r.session.execute(update(Chapter).where(Chapter.id == UUID(r.source.chapter_id)).values(global_idx=902))
            elif mutation == 'volume_order': r.session.execute(update(Volume).values(volume_idx=3))
            elif mutation == 'project':
                pid = uuid4()
                r.session.execute(insert(Project).values(id=pid, title='另一部作品'))
                r.session.execute(update(Volume).values(project_id=pid))
            else: r.session.execute(update(Chapter).where(Chapter.id == UUID(target.chapter_id)).values(global_idx=902))
            r.session.commit()
        return headers
    monkeypatch.setattr(entity_sources, 'resolve_entity_source_headers', mutate_after_header)
    with pytest.raises(RequiredContextUnavailable): await load(r, before_chapter_id=target.chapter_id)


@pytest.mark.asyncio
async def test_body_change_between_header_and_cold_proof_is_rejected(hashed_db, monkeypatch):
    r = hashed_db
    await run(r)
    actual = validation.resolve_entity_sources
    async def changed(db, pid, ids, **kwargs):
        r.session.execute(update(Chapter).values(content_text='新稿仍有旧引文：' + r.source.content)); r.session.commit()
        return await actual(db, pid, ids)
    monkeypatch.setattr(validation, 'resolve_entity_sources', changed)
    with pytest.raises(RequiredContextUnavailable, match='source_unavailable'): await load(r)


@pytest.mark.asyncio
async def test_warm_cache_does_not_skip_missing_old_chapter_or_duplicate_order(hashed_db):
    r = hashed_db
    await run(r)
    await load(r)
    earlier = await add_state(r, 899, {}, body='前章没有完成认知提取。')
    with pytest.raises(RequiredContextUnavailable, match='not_ready'): await load(r)
    save_payload(r, earlier, [])
    assert (await load(r))['陈川']['history']
    r.session.execute(insert(Chapter).values(id=uuid4(), volume_id=r.vid, chapter_idx=4,
        global_idx=900, title='重复但未写', status='draft')); r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='source_unavailable'): await load(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('index', [None, 0, -1])
async def test_completed_source_with_unknown_position_is_not_silently_omitted(hashed_db, index):
    r = hashed_db
    await run(r)
    await load(r)
    r.session.execute(insert(Chapter).values(id=uuid4(), volume_id=r.vid, chapter_idx=2,
        global_idx=index, title='位置尚未恢复', status='completed', content_text='应先核实这章的归属位置。'))
    r.session.commit()
    with pytest.raises(RequiredContextUnavailable, match='boundary'): await load(r)


@pytest.mark.asyncio
async def test_summary_only_update_preserves_exact_body_proof(hashed_db):
    r = hashed_db
    await run(r)
    before = await load(r)
    r.session.execute(update(Chapter).values(summary='更新摘要', word_count=100)); r.session.commit()
    r.body_reads.clear()
    assert await load(r) == before and not r.body_reads


def test_evicted_or_disabled_cache_does_not_claim_a_proof():
    cache = validation.EvidenceProofCache(max_entries=2)
    cache.remember('a'); cache.remember('b')
    assert cache.contains('a')
    cache.remember('c')
    assert not cache.contains('b') and cache.contains('a') and cache.contains('c')
    cache.clear()
    assert not cache.contains('a')
    disabled = validation.EvidenceProofCache(max_entries=0)
    disabled.remember('a')
    assert not disabled.contains('a')


@pytest.mark.asyncio
async def test_evicted_proof_revalidates_full_quote_instead_of_dropping_history(hashed_db, monkeypatch):
    r = hashed_db
    await run(r)
    first = await load(r)
    monkeypatch.setattr(validation, 'evidence_proofs', validation.EvidenceProofCache(max_entries=0))
    r.body_reads.clear()
    assert await load(r) == first
    assert r.body_reads == [dict(rows=1, chars=len(r.source.content))]


@pytest.mark.asyncio
async def test_five_million_character_book_reads_changed_bodies_only_when_warm(hashed_db):
    r = hashed_db
    await run(r)
    quote = '村里那口井已经封上了。'
    body = quote + '远处的雨没有停。' * 630
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i + 1, global_idx=900 + i,
        title=f'第{i + 1}章', status='completed', content_text=body) for i in range(1, 1005)]
    r.session.execute(insert(Chapter), rows); r.session.commit()
    total_chars = len(body) * len(rows) + len(r.source.content)
    assert total_chars >= 5_000_000
    records = []
    for row in rows:
        source = entity_sources._source_from_row(NS(**row, project_id=r.pid, volume_idx=2))
        changes = [dict(character=f'路人{i}', learns=f'第{row["global_idx"]}处的旧井被封', evidence=quote) for i in range(5)]
        records.append(dict(id=uuid4(), project_id=r.pid, chapter_id=row['id'], source_hash=source.source_hash,
            source_json=source.receipt, status='completed', changes_json=changes, payload_hash=digest([source.source_hash, changes])))
    r.session.execute(insert(Record), records); r.session.commit()
    r.cache.clear(); r.body_reads.clear(); r.queries.clear()
    started = perf_counter()
    cold = await history.load_prompt_context(r.db, r.pid, before_global_idx=1905,
        chapter_text='陈川和沈青见面。', priority_names=['陈川'])
    cold_seconds = perf_counter() - started
    assert sum(read['chars'] for read in r.body_reads) == total_chars
    assert sum(read['rows'] for read in r.body_reads) == 1005
    assert max(read['rows'] for read in r.body_reads) <= 200
    assert r.changes[0]['evidence'] in cold
    cold_queries = len(r.queries)
    r.body_reads.clear(); r.queries.clear()
    started = perf_counter()
    warm = await history.load_prompt_context(r.db, r.pid, before_global_idx=1905,
        chapter_text='陈川和沈青见面。', priority_names=['陈川'])
    warm_seconds = perf_counter() - started
    assert warm == cold and not r.body_reads
    warm_queries = len(r.queries)
    # One modified source with a newly completed delta, while 1004 stay valid.
    changed_body = body + '后来，村民又拆掉了封条。'
    r.session.execute(update(Chapter).where(Chapter.id == rows[-1]['id']).values(content_text=changed_body)); r.session.commit()
    source = await resolve_entity_source(r.db, r.pid, 0, rows[-1]['id'])
    save_payload(r, source, [dict(character='路人0', learns='井口封条被拆掉', evidence='后来，村民又拆掉了封条。')])
    r.body_reads.clear()
    changed = await load(r, before=1905)
    assert changed['路人0']['history'][-1]['fact'] == '井口封条被拆掉'
    assert r.body_reads == [dict(rows=1, chars=len(changed_body))]
    print(json.dumps(dict(characters=total_chars, chapters=1005, observations=5021,
        cold_body_characters=total_chars, warm_body_characters=0,
        changed_body_characters=len(changed_body), cold_queries=cold_queries, warm_queries=warm_queries,
        cold_seconds=round(cold_seconds, 3), warm_seconds=round(warm_seconds, 3))))
