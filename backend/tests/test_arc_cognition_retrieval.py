"""Actual arc selection with SQLite and local Qdrant, never a semantic model eval."""
import json
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.models.cognition import ChapterCognition as Record
from app.models.project import Chapter
from app.services import arc_planning_execution as execution, cognition_validation, entity_sources
from app.services.arc_planning_sources import read_planning_source
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest
from app.services.planning_cognition_retrieval import prepare_planning_cognition
from app.services.source_proof_cache import EvidenceProofCache
from tests.support.arc_planning_runtime import planning_book, request_body, run, snapshot, submit
from tests.support.planning_cognition import cognition_values, seed_cognition
from tests.test_arc_cognition_material import cognition_book, cognition_material
from tests.test_cognition_retrieval import vector_db


class LetterSpace:
    """Deterministic vectors test selection plumbing, not Chinese relevance."""
    identity = digest('offline-letter-cognition-space-v1')
    def __init__(self, hook=None): self.calls, self.hook = [], hook
    async def embed(self, texts, *, input_type):
        assert 1 <= len(texts) <= 32 and all(0 < len(text) <= 4000 for text in texts)
        self.calls.append((input_type, list(texts)))
        if self.hook is not None: await self.hook(input_type, texts)
        if input_type == 'query': return [[1.0, 0.0] for _ in texts]
        return [[1.0, 0.0] if '信' in text else [0.1, 0.9] for text in texts]


@pytest.mark.asyncio
async def test_actual_five_million_character_arc_retains_old_unknown_and_later_learning_without_repeated_history_io(
        cognition_book, vector_db, monkeypatch, record_testsuite_property):
    r = cognition_book; first = await cognition_material(r)
    monkeypatch.setattr(cognition_validation, 'evidence_proofs', EvidenceProofCache())
    padding = '船舱的旧账还没有核完，今日仍照约定登记实数。' * 240
    rows, changes = [], {}
    for index in range(2, 1007):
        identity = uuid4()
        lines = [f'邻人{index}_{j}看见檐角在滴水。' for j in range(5)]
        changes[str(identity)] = [dict(character=f'邻人{index}_{j}', learns='檐角在滴水', evidence=line) for j, line in enumerate(lines)]
        if index == 1006:
            latest = '林禾接过陈川退回的信，这才知道信还没有送到船主手里。'
            lines.append(latest)
            changes[str(identity)].extend([dict(character='林禾', learns='信还没有送到船主手里', evidence=latest),
                dict(character='__reader__', learns='陈川把信还给了林禾', evidence=latest)])
        rows.append(dict(id=identity, volume_id=r.vid, chapter_idx=index, global_idx=index,
            title=f'第{index}章', status='completed', content_text='\n'.join(lines) + '\n' + padding))
    with r.engine.begin() as db: db.execute(insert(Chapter), rows)
    async with r.sessions() as db:
        sources = await entity_sources.resolve_entity_sources(db, r.pid, [row['id'] for row in rows])
    with r.engine.begin() as db:
        db.execute(insert(Record), [cognition_values(source, changes[cid]) for cid, source in sources.items()])
    # These reference profiles reach the model, but must not make every
    # unrelated library name a mandatory actor in a 12000-character excerpt.
    r.character_graph.profiles = [dict(node_id=f'node-{i}', id=str(uuid4()), name=f'邻人{i}_0',
        profile_json='{}', profile_origin='author_edit') for i in range(2, 202)]
    total = len(first.source.content) + sum(len(row['content_text']) for row in rows)
    assert total >= 5_000_000
    reads = []
    original_reader = entity_sources.resolve_entity_sources
    async def measured(*args, **kwargs):
        result = await original_reader(*args, **kwargs)
        if result: reads.append(dict(rows=len(result), chars=sum(len(s.content) for s in result.values())))
        return result
    monkeypatch.setattr(entity_sources, 'resolve_entity_sources', measured)
    monkeypatch.setattr(cognition_validation, 'resolve_entity_sources', measured)
    space, choices, historical_per_attempt, selection_seconds = LetterSpace(), [], [], []
    original_prepare = execution.prepare_planning_cognition
    async def loader(): return space
    async def prepare(source, query, *, db, sessions):
        assert not db.in_transaction() and not db.bind.in_transaction()
        choices.append(query)
        before = sum(len(texts) for kind, texts in space.calls if kind == 'passage')
        started = perf_counter()
        result = await original_prepare(source, query, db=db, sessions=sessions, client=vector_db, space_loader=loader)
        selection_seconds.append(perf_counter() - started)
        historical_per_attempt.append(sum(len(texts) for kind, texts in space.calls if kind == 'passage') - before)
        return result
    monkeypatch.setattr(execution, 'prepare_planning_cognition', prepare)
    start = perf_counter()
    body = await request_body(r, target=11, core_setup='林禾与陈川核对信的去向，据实际进项商量还款。')
    preview_seconds = perf_counter() - start
    cold_reads = list(reads); reads.clear()
    assert len(cold_reads) == 6 and sum(item['chars'] for item in cold_reads) == total
    start = perf_counter()
    assert (await submit(r, body)).status_code == 202
    await run(r, body['request_id'])
    planning_seconds = perf_counter() - start
    state = snapshot(r, body['request_id'])
    assert state['status'] == 'completed' and len(r.calls) == len(choices) == 4
    assert not reads
    assert all(first.changes[0]['evidence'] in user and latest in user for _, user, _ in r.calls)
    assert all('邻人200_0' not in query for query in choices)
    assert all('邻人200_0' in user for _, user, _ in r.calls)
    source = state['source_json']['cognition_context']
    events = sum(len(row['history']) for row in source['ledger'].values())
    assert events == 5032 and len(source['snapshot']['positions']) == 1006
    assert historical_per_attempt == [events, 0, 0, 0]
    attempts = [a for s in state['journal_json']['stages'] for a in s['attempts']]
    assert all(a['cognition']['space_id'] == space.identity and a['cognition']['ranked_keys'] for a in attempts)
    record_testsuite_property('arc_cognition_scale', json.dumps(dict(chapters=1006, prose_characters=total,
        cognition_events=events, cold_body_batches=len(cold_reads), subsequent_body_characters=0,
        historical_embeddings_per_attempt=historical_per_attempt, preview_seconds=preview_seconds,
        planning_seconds=planning_seconds, selection_seconds=selection_seconds)))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['decision', 'body', 'boundary'])
async def test_current_sources_are_rechecked_after_real_local_retrieval(cognition_book, vector_db, change):
    r = cognition_book; first = await cognition_material(r)
    lines = [f'旁人{i}看见河边灯亮了。' for i in range(350)]
    prose = first.source.content + '\n' + '\n'.join(lines)
    observations = first.changes + [dict(character=f'旁人{i}', learns='河边灯亮了', evidence=line) for i, line in enumerate(lines)]
    with r.engine.begin() as db: db.execute(update(Chapter).where(Chapter.id == r.cid).values(content_text=prose))
    current = await seed_cognition(r, r.cid, observations)
    async with r.sessions() as db: source = (await read_planning_source(db, r.pid))['cognition_context']
    changed = False
    async def mutate(kind, texts):
        nonlocal changed
        if changed: return
        changed = True
        with r.engine.begin() as db:
            if change == 'decision':
                db.execute(update(Record).where(Record.id == current.record_id).values(changes_json=[],
                    payload_hash=digest([current.source.source_hash, []])))
            elif change == 'body': db.execute(update(Chapter).where(Chapter.id == r.cid).values(content_text='作者的新稿。'))
            else: db.execute(update(Chapter).where(Chapter.id == r.cid).values(global_idx=2))
    space = LetterSpace(mutate)
    async def loader(): return space
    with pytest.raises(RequiredContextUnavailable):
        await prepare_planning_cognition(source, '林禾核对信的去向。', sessions=r.sessions, client=vector_db, space_loader=loader)
    assert changed and not r.calls
