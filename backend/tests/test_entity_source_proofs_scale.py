"""Measure actual source reads and arc calls; no provider or service access."""
import json
from time import perf_counter
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.models.project import Chapter
from app.services import entity_sources, entity_source_validation as validation
from app.services.planning_character_sources import read_planning_character_source
from app.services.source_proof_cache import EvidenceProofCache
from tests.support.body_hashes import install_sqlite_body_hashes
from tests.support.arc_planning_runtime import planning_book, request_body, run, snapshot, submit
from tests.test_arc_character_material import character_book, character_material


@pytest.fixture
def hashed_graph_book(character_book, monkeypatch):
    r = character_book
    install_sqlite_body_hashes(r.engine)
    monkeypatch.setattr(validation, 'entity_evidence_proofs', EvidenceProofCache())
    r.body_reads = []
    return r


async def measured_source(r):
    async with r.sessions() as db:
        class Reader:
            async def execute(self, statement):
                result = await db.execute(statement)
                if any(getattr(col, 'key', None) == 'content_text' for col in statement.selected_columns):
                    rows = result.all()
                    r.body_reads.append(dict(rows=len(rows), chars=sum(len(row.content_text or '') for row in rows)))
                    return NS(all=lambda: rows)
                return result
        return await read_planning_character_source(Reader(), r.pid, 6)


@pytest.mark.asyncio
async def test_five_million_character_sources_reuse_proofs_and_reread_only_the_changed_chapter(hashed_graph_book, record_testsuite_property):
    r = hashed_graph_book; data = await character_material(r)
    body = '船舱中的借据尚未核完，林禾仍须按实际进项还款。' * 240
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i, global_idx=i, title=f'第{i}章',
        content_text=body, status='completed') for i in range(2, 1007)]
    with r.engine.begin() as db: db.execute(insert(Chapter), rows)
    async with r.sessions() as db:
        sources = await entity_sources.resolve_entity_sources(db, r.pid, [row['id'] for row in rows])
    for source in sources.values():
        r.character_graph.states.append(dict(name='林禾', status=json.dumps({'账目进度': f'核至第{source.global_idx}章'}),
            source_chapter_id=source.chapter_id, source_hash=source.source_hash, global_idx=source.global_idx))
    total = len(data.body) + len(body) * len(rows)
    assert total >= 5_000_000
    start = perf_counter(); cold = await measured_source(r); cold_seconds = perf_counter() - start
    cold_reads = list(r.body_reads); r.body_reads.clear()
    start = perf_counter(); warm = await measured_source(r); warm_seconds = perf_counter() - start
    assert warm == cold and not r.body_reads
    assert sum(row['chars'] for row in cold_reads) == total and len(cold_reads) == 6
    assert max(row['rows'] for row in cold_reads) <= 200
    assert cold['states'][0]['field_sources']['左手']['global_idx'] == 1
    assert cold['states'][0]['field_sources']['账目进度']['global_idx'] == 1006

    changed_body = body + '\n林禾核清最后一张借据，余款仍未交付。'
    last = rows[-1]['id']
    with r.engine.begin() as db:
        db.execute(update(Chapter).where(Chapter.id == last).values(content_text=changed_body))
    async with r.sessions() as db:
        new = await entity_sources.resolve_entity_source(db, r.pid, 0, last)
    for row in r.character_graph.states:
        if row['global_idx'] == 1006:
            row.update(source_hash=new.source_hash, status='{"账目进度":"最后一张已核清，余款尚未交付"}')
    r.body_reads.clear()
    start = perf_counter(); changed = await measured_source(r); changed_seconds = perf_counter() - start
    assert r.body_reads == [dict(rows=1, chars=len(changed_body))]
    assert changed['states'][0]['status']['账目进度'] == '最后一张已核清，余款尚未交付'
    assert changed['states'][0]['status']['左手'] == cold['states'][0]['status']['左手']
    assert changed['states'][0]['field_sources']['账目进度']['source_hash'] == new.source_hash
    assert all('content_text' not in row for row in changed['chapter_order'])
    record_testsuite_property('entity_proof_scale', json.dumps(dict(chapters=1006, prose_characters=total,
        cold_body_batches=len(cold_reads), warm_body_characters=0, changed_body_characters=len(changed_body),
        cold_seconds=cold_seconds, warm_seconds=warm_seconds, changed_seconds=changed_seconds)))


@pytest.mark.asyncio
async def test_actual_arc_preview_submission_and_all_stages_reuse_only_source_proofs(hashed_graph_book, monkeypatch):
    r = hashed_graph_book; data = await character_material(r)
    original = entity_sources.resolve_entity_sources
    reads = []
    async def measured(*args, **kwargs):
        sources = await original(*args, **kwargs)
        if sources: reads.append(sum(len(source.content) for source in sources.values()))
        return sources
    monkeypatch.setattr(entity_sources, 'resolve_entity_sources', measured)
    body = await request_body(r, target=11)
    await submit(r, body); await run(r, body['request_id'])
    assert snapshot(r, body['request_id'])['status'] == 'completed' and len(r.calls) == 4
    assert reads == [len(data.body)]
    assert all('暂不能握紧绳索' in user and data.body in user for _, user, _ in r.calls)
