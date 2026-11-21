"""SQL source freshness during retrieval, including empty extraction receipts."""
import json
from time import perf_counter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, update

from app.models.cognition import ChapterCognition as Record
from app.models.project import Chapter
from app.services import character_cognition as history, cognition_retrieval as retrieval, entity_sources
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, resolve_entity_source
from app.services.qdrant_store import cognition_collection
from tests.test_cognition_validation import hashed_db, cognition_db, state_db, run, save_payload, add_state
from tests.test_cognition_author_edits import decide
from tests.test_cognition_retrieval import FixedSpace, vector_db


async def populated(r):
    changes = r.changes + [dict(character=f'路人{i}', learns=f'第{i}件收信消息。',
        evidence=r.changes[0]['evidence']) for i in range(75)]
    r.model.return_value = NS(text=json.dumps(changes, ensure_ascii=False), finish_reason='stop')
    await run(r)
    return changes


@pytest.mark.asyncio
async def test_full_collection_then_retrieval_and_recheck_use_separate_sessions(hashed_db, vector_db):
    r = hashed_db
    await populated(r)
    async with r.sessions() as db:
        request = await history.load_prompt_context(db, r.pid, before_global_idx=901,
            chapter_text='陈川打听水路。', defer_retrieval=True)
    assert isinstance(request, retrieval.CognitionPrompt)
    async def assert_closed(kind, texts): assert not r.opened
    space = FixedSpace(assert_closed)
    result = await retrieval.resolve_prompt_context(request, client=vector_db, sessions=r.sessions,
        space_loader=AsyncMock(return_value=space))
    assert r.changes[0]['evidence'] in result and not r.opened
    assert any(kind == 'passage' for kind, _ in space.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['author', 'raw_payload', 'body', 'delete', 'position', 'empty_source'])
async def test_changes_during_embedding_invalidate_the_entire_prepared_snapshot(hashed_db, vector_db, mutation):
    r = hashed_db
    await populated(r)
    empty = await add_state(r, 901, {}, body='这一夜没有新的消息。')
    save_payload(r, empty, [])
    request = await history.load_prompt_context(r.db, r.pid, before_global_idx=903,
        chapter_text='陈川打听水路。', defer_retrieval=True)
    original_ledger = dict(request.ledger)
    r.session.rollback()
    changed = False
    async def mutate(kind, texts):
        nonlocal changed
        assert not r.opened
        if changed: return
        changed = True
        if mutation == 'author': await decide(r, [])
        elif mutation == 'raw_payload':
            r.session.execute(update(Record).where(Record.chapter_id == UUID(r.source.chapter_id))
                .values(changes_json=[], payload_hash=digest([r.source.source_hash, []])))
            r.session.commit()
        elif mutation == 'empty_source':
            r.session.execute(update(Chapter).where(Chapter.id == UUID(empty.chapter_id)).values(content_text='第二夜依然没有消息。'))
            r.session.commit()
            new_source = await resolve_entity_source(r.db, r.pid, 0, empty.chapter_id)
            save_payload(r, new_source, [])
            assert dict(await history.load_ledger(r.db, r.pid, before_global_idx=903)) == original_ledger
            r.session.rollback()
        else:
            statement = delete(Chapter) if mutation == 'delete' else update(Chapter).values(
                **({'global_idx': 904} if mutation == 'position' else {'content_text': '作者已经重写。'}))
            r.session.execute(statement.where(Chapter.id == UUID(r.source.chapter_id)))
            r.session.commit()
    with pytest.raises(RequiredContextUnavailable):
        await retrieval.resolve_prompt_context(request, client=vector_db, sessions=r.sessions,
            space_loader=AsyncMock(return_value=FixedSpace(mutate)))
    assert changed


@pytest.mark.asyncio
async def test_target_move_cannot_reuse_an_unchanged_numeric_history_window(hashed_db, vector_db):
    r = hashed_db
    await populated(r)
    target = await add_state(r, 902, {}, body='当前目标章。')
    following = await add_state(r, 903, {}, body='未来章。')
    request = await history.load_prompt_context(r.db, r.pid, before_global_idx=902,
        before_chapter_id=target.chapter_id, chapter_text='陈川回来。', defer_retrieval=True)
    r.session.rollback()
    changed = False
    async def swap(kind, texts):
        nonlocal changed
        if changed: return
        changed = True
        r.session.execute(update(Chapter).where(Chapter.id == UUID(target.chapter_id)).values(global_idx=903))
        r.session.execute(update(Chapter).where(Chapter.id == UUID(following.chapter_id)).values(global_idx=902))
        r.session.commit()
        # The old numeric window still contains the same earlier chapter.
        assert dict(await history.load_ledger(r.db, r.pid, before_global_idx=902)) == dict(request.ledger)
        r.session.rollback()
    with pytest.raises(RequiredContextUnavailable, match='boundary_unavailable'):
        await retrieval.resolve_prompt_context(request, client=vector_db, sessions=r.sessions,
            space_loader=AsyncMock(return_value=FixedSpace(swap)))


@pytest.mark.asyncio
async def test_target_id_and_supplied_original_position_must_agree(hashed_db):
    r = hashed_db
    target = await add_state(r, 902, {}, body='当前目标章。')
    with pytest.raises(RequiredContextUnavailable, match='boundary_unavailable'):
        await history.load_prompt_context(r.db, r.pid, before_global_idx=901,
                                          before_chapter_id=target.chapter_id, defer_retrieval=True)


@pytest.mark.asyncio
async def test_new_candidate_reloads_author_decisions_and_uses_its_actual_text(hashed_db, vector_db, monkeypatch):
    r = hashed_db
    await populated(r)
    target = await add_state(r, 901, {}, body='当前目标。')
    space = FixedSpace()
    real_resolve = retrieval.resolve_prompt_context
    async def resolve(value, **kwargs):
        assert not r.opened
        return await real_resolve(value, client=vector_db, space_loader=AsyncMock(return_value=space), **kwargs)
    monkeypatch.setattr(retrieval, 'resolve_prompt_context', resolve)
    args = dict(before_chapter_id=target.chapter_id, chapter_outline={'summary': '收信'}, sessions=r.sessions)
    first = await retrieval.load_candidate_context(r.pid, chapter_text='陈川读信。第一轮独有。', **args)
    assert r.changes[0]['evidence'] in first
    assert any('第一轮独有' in text for kind, texts in space.calls if kind == 'query' for text in texts)
    # A same-source author decision between rounds must replace the first
    # excerpt. A short history needs no embeddings but is still freshly read.
    await decide(r, [dict(character='陈川', suspects='父亲可能已经收信', evidence=r.changes[0]['evidence'])])
    space.calls.clear()
    second = await retrieval.load_candidate_context(r.pid, chapter_text='陈川开始怀疑。第二轮独有。', **args)
    assert '当时怀疑' in second and '作者校订' in second and second != first
    assert not space.calls


@pytest.mark.asyncio
async def test_five_million_characters_are_indexed_completely_and_warm_passages_reused(hashed_db, vector_db):
    r = hashed_db
    await run(r)
    quote = '村里那口井已经封上了。'
    body = quote + '远处的雨没有停。' * 630
    chapters = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=i + 1, global_idx=900 + i,
        title=f'第{i + 1}章', status='completed', content_text=body) for i in range(1, 1005)]
    r.session.execute(insert(Chapter), chapters); r.session.commit()
    total_chars = len(body) * len(chapters) + len(r.source.content)
    assert total_chars >= 5_000_000
    records = []
    for row in chapters:
        source = entity_sources._source_from_row(NS(**row, project_id=r.pid, volume_idx=2))
        changes = [dict(character=f'路人{i}', learns=f'第{row["global_idx"]}处的旧井被封', evidence=quote) for i in range(5)]
        records.append(dict(id=uuid4(), project_id=r.pid, chapter_id=row['id'], source_hash=source.source_hash,
            source_json=source.receipt, status='completed', changes_json=changes, payload_hash=digest([source.source_hash, changes])))
    r.session.execute(insert(Record), records); r.session.commit()
    target_id = uuid4()
    r.session.execute(insert(Chapter).values(id=target_id, volume_id=r.vid, chapter_idx=1006,
        global_idx=1905, title='下一章目标', status='draft', content_text=''))
    r.session.commit()
    r.cache.clear(); r.body_reads.clear(); r.queries.clear()
    space = FixedSpace()
    # A fixed vector double singles out an early event. This proves selection
    # and data plumbing, not the quality of a real embedding model.
    embed = space.embed
    async def select_early(texts, *, input_type):
        await embed(texts, input_type=input_type)
        return [[1.0, 0.0] if input_type == 'query' or '第901处的旧井' in text else [0.1, 0.9] for text in texts]
    space.embed = select_early

    async def prepare():
        pending = await history.load_prompt_context(r.db, r.pid, before_global_idx=1905,
            before_chapter_id=target_id,
            chapter_text='陈川打听村里的水路。' + '他们沿着河岸继续走。' * 950,
            previous_text=body, priority_names=['陈川'], defer_retrieval=True)
        r.session.rollback()
        return await retrieval.resolve_prompt_context(pending, client=vector_db, sessions=r.sessions,
            space_loader=AsyncMock(return_value=space))

    start = perf_counter()
    cold = await prepare()
    cold_seconds = perf_counter() - start
    cold_queries = len(r.queries)
    assert '[CH-901]' in cold and '[CH-1904]' in cold and r.changes[0]['evidence'] in cold
    assert sum(read['chars'] for read in r.body_reads) == total_chars
    count = (await vector_db.count(cognition_collection(r.pid, space.identity, 2), exact=True)).count
    assert count == 5021
    passage_batches = [texts for kind, texts in space.calls if kind == 'passage']
    assert len(passage_batches) == 157 and sum(map(len, passage_batches)) == count
    assert sum(len(texts) for kind, texts in space.calls if kind == 'query') >= 4
    space.calls.clear(); r.body_reads.clear(); r.queries.clear()
    start = perf_counter()
    warm = await prepare()
    warm_seconds = perf_counter() - start
    assert warm == cold and not r.body_reads
    assert not any(kind == 'passage' for kind, _ in space.calls)
    print(json.dumps(dict(characters=total_chars, chapters=1005, target_chapters=1, observations=count, passage_batches=157,
        warm_passage_embeddings=0, warm_prose_characters=0, cold_sql=cold_queries, warm_sql=len(r.queries),
        cold_seconds=round(cold_seconds, 3), warm_seconds=round(warm_seconds, 3))))
    # A complete old encoder can be retired without embedding again or
    # dropping any current event, even when the book exceeds 5 million chars.
    from app.services import cognition_index_maintenance as maintenance
    from qdrant_client.models import Distance, PointStruct, VectorParams
    current = await maintenance.load_index_snapshot(r.sessions, str(r.pid))
    old_space = digest('retired-scale-encoder')
    old_name = cognition_collection(r.pid, old_space, 2)
    await vector_db.create_collection(old_name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    obsolete = retrieval.index_entries(current, old_space)
    await vector_db.upsert(old_name, [PointStruct(id=pid, vector=[1.0, 0.0], payload=row['payload'])
        for pid, row in obsolete.items()], wait=True)
    space.calls.clear(); r.body_reads.clear(); r.queries.clear()
    start = perf_counter()
    cleaned = await maintenance.cleanup_project_indexes(r.pid, r.sessions, client=vector_db,
        space_loader=AsyncMock(return_value=space))
    elapsed = perf_counter() - start
    assert cleaned['status'] == 'complete' and cleaned['removed_points'] == 5021
    assert cleaned['removed_collections'] == 1 and not await vector_db.collection_exists(old_name)
    assert (await vector_db.count(cognition_collection(r.pid, space.identity, 2), exact=True)).count == 5021
    assert not space.calls and not r.body_reads
    print(json.dumps(dict(cleanup_scanned=cleaned['scanned_points'], cleanup_removed=cleaned['removed_points'],
        cleanup_sql=len(r.queries), cleanup_seconds=round(elapsed, 3), cleanup_embeddings=0, cleanup_prose_characters=0)))
