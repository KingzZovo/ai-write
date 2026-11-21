"""Exercise chronology filters with actual Qdrant local query execution."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.services import qdrant_store as qs


async def seed(client, name, payloads):
    await client.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    await client.upsert(name, [PointStruct(id=i, vector=[1.0, 0.0], payload=p) for i, p in enumerate(payloads)])


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_summaries_filter_prior_current_future_unknown_and_other_project(legacy):
    client = AsyncQdrantClient(':memory:')
    try:
        live = qs.LEGACY_CHAPTER_SUMMARIES if legacy else qs.chapter_summaries_collection('book')
        compact = qs.LEGACY_COMPACTED_SUMMARIES if legacy else qs.compacted_summaries_collection('book')
        payloads = [{'project_id': 'book', 'global_idx': n, 'chapter_idx': 1, 'summary': str(n)} for n in [2, 11, 12, 21, None, 0, -1, True, '3', 1.5]]
        payloads.append({'project_id': 'book', 'global_idx': 3, 'compacted': True})
        if legacy:
            payloads.append({'project_id': 'other', 'global_idx': 1})
        await seed(client, live, payloads)
        await seed(client, compact, [{'project_id': 'book', 'source_global_idx_max': n, 'summary': str(n)} for n in [10, 12, 20, None]])
        hits = await qs.search_project_summaries(client, 'book', [1.0, 0.0], before_global_idx=12, limit=20)
        assert {(h['tier'], h['payload']['summary']) for h in hits} == {('live', '2'), ('live', '11'), ('compacted', '10')}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chunks_reject_current_future_and_missing_chronology():
    client = AsyncQdrantClient(':memory:')
    try:
        await seed(client, qs.chapter_chunks_collection('book'), [{'global_idx': n} for n in [1, 11, 12, 13, None, True, '2', 2.5]])
        hits = await qs.search_project_chunks(client, 'book', [1.0, 0.0], before_global_idx=12, limit=20)
        assert {h['payload']['global_idx'] for h in hits} == {1, 11}
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [0, -1, True, '12', 12.5])
async def test_invalid_target_performs_no_search(boundary):
    client = AsyncMock()
    assert await qs.search_project_chunks(client, 'book', [1.0, 0.0], before_global_idx=boundary) == []
    assert await qs.search_project_summaries(client, 'book', [1.0, 0.0], before_global_idx=boundary) == []
    assert not client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('summary_result', [{'summary': '合并'}, {}, {'summary': '  '}, {'summary': ['错误类型']}, ['非对象'], '非结构化文本'])
async def test_compaction_reads_all_pages_orders_globally_and_retains_unknown(monkeypatch, summary_result):
    from app.services import memory_compactor as mc
    async def verified(_db, _project, hits, **kwargs):
        return hits
    monkeypatch.setattr('app.services.vector_provenance.filter_current_memory', verified)
    client = AsyncMock()
    # The late-volume CH-1 must remain recent despite its low local index.
    points = [SimpleNamespace(id=n, payload={'global_idx': n, 'chapter_idx': 1 if n >= 10 else n, 'summary': f'事件{n}'}) for n in [11, 2, 10, 1, 3]]
    unknown = SimpleNamespace(id=99, payload={'chapter_idx': 1, 'summary': '未知'})
    client.scroll.side_effect = [(points[:2], 'next'), (points[2:] + [unknown], None)]
    monkeypatch.setattr(mc, 'AsyncQdrantClient', lambda **kw: client)
    monkeypatch.setattr(qs, 'resolve_summary_read', AsyncMock(return_value=('live', False)))
    monkeypatch.setattr(qs, 'ensure_summary_shard', AsyncMock(return_value='compact'))
    prompt = AsyncMock(return_value=summary_result)
    monkeypatch.setattr(mc, 'run_structured_prompt', prompt)
    monkeypatch.setattr(mc, 'generate_embedding', AsyncMock(return_value=[1.0, 0.0]))
    result = await mc.compact_project_memory(project_id='book', db=AsyncMock(), force=True)
    if summary_result != {'summary': '合并'}:
        assert result['compacted'] == 0
        mc.generate_embedding.assert_not_awaited()
        client.upsert.assert_not_awaited()
        client.set_payload.assert_not_awaited()
        return
    assert result['compacted'] == 4
    assert result['unknown_chronology'] == 1
    assert client.scroll.await_args_list[1].kwargs['offset'] == 'next'
    point = client.upsert.await_args.kwargs['points'][0]
    assert point.payload['source_global_idx_min'] == 1
    assert point.payload['source_global_idx_max'] == 10
    assert client.set_payload.await_args.kwargs['points'] == [1, 2, 3, 10]
    assert '[CH-10]' in prompt.await_args.args[1]
    assert '未知' not in prompt.await_args.args[1].split('</原始摘要组>')[0]


@pytest.mark.asyncio
async def test_unknown_boundary_keeps_reference_book_recall(monkeypatch):
    from app.services.context_pack import ContextPack, ContextPackBuilder
    monkeypatch.setattr('app.services.feature_extractor.generate_embedding', AsyncMock(return_value=[1.0, 0.0]))
    client = AsyncMock()
    monkeypatch.setattr('qdrant_client.AsyncQdrantClient', lambda **kw: client)
    monkeypatch.setenv('CONTEXT_PACK_V2_ENABLED', 'true')
    builder = ContextPackBuilder(db=AsyncMock())
    reference = AsyncMock()
    monkeypatch.setattr(builder, '_v2_three_way_recall', reference)
    pack = ContextPack()
    await builder._search_qdrant_snippets(pack, ['信'], 'book')
    assert pack.rag_snippets == [] and pack.chunk_recall == []
    client.search.assert_not_awaited()
    reference.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('value, expected', [(None, 0), (0, 0), (True, 0), (51, 51)])
async def test_target_boundary_uses_persisted_index_and_fails_closed(value, expected):
    from app.services.memory_chronology import resolve_history_boundary
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: value)
    from uuid import uuid4
    assert await resolve_history_boundary(db, str(uuid4()), str(uuid4()), 2) == expected
    sql = str(db.execute.await_args.args[0])
    assert 'volumes.project_id' in sql and 'chapters.volume_id' in sql and 'chapters.chapter_idx' in sql


@pytest.mark.asyncio
async def test_ambiguous_target_cannot_fall_back_to_local_index():
    from app.services.memory_chronology import resolve_history_boundary
    from uuid import uuid4
    db = AsyncMock()
    db.execute.side_effect = RuntimeError('ambiguous')
    assert await resolve_history_boundary(db, str(uuid4()), str(uuid4()), 2) == 0
    db.execute.assert_awaited_once()


from tests.test_pipeline_control import runtime_db


@pytest.mark.asyncio
async def test_database_boundary_rejects_foreign_missing_and_duplicate_targets(runtime_db):
    from uuid import uuid4
    from app.models.project import Chapter
    from app.services.memory_chronology import resolve_history_boundary
    r = runtime_db
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        chapter.global_idx = 51  # Persisted index differs from live chapter counts.
        await db.commit()
        args = (db, str(r.project), str(chapter.volume_id), chapter.chapter_idx)
        assert await resolve_history_boundary(*args) == 51
        assert await resolve_history_boundary(db, str(uuid4()), str(chapter.volume_id), 1) == 0
        assert await resolve_history_boundary(db, str(r.project), str(chapter.volume_id), 99) == 0
        db.add(Chapter(volume_id=chapter.volume_id, chapter_idx=1, global_idx=52, title='重复编号'))
        await db.commit()
        assert await resolve_history_boundary(*args) == 0


@pytest.mark.asyncio
async def test_legacy_volume_memory_excludes_current_future_and_foreign(runtime_db, monkeypatch):
    from uuid import uuid4
    from app.models.project import Chapter, Volume, VolumeSummary
    from app.services.memory import HierarchicalMemory
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: VolumeSummary.__table__.create(sync, checkfirst=True))
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        current = await db.get(Volume, chapter.volume_id)
        current.volume_idx = 2
        chapter.global_idx = 2
        previous = Volume(project_id=r.project, volume_idx=1, title='前卷')
        future = Volume(project_id=r.project, volume_idx=3, title='后卷')
        db.add_all([previous, future])
        await db.flush()
        for vol, text in [(previous, '已发生'), (current, '本卷结局'), (future, '未来结局')]:
            db.add(VolumeSummary(volume_id=vol.id, summary_text=text))
        from app.services.summary_provenance import summary_source_hash
        from app.services.aggregate_memory import generate_volume_memory
        prior_ch = Chapter(volume_id=previous.id, chapter_idx=1, global_idx=1, title='前章',
                           content_text='已发生正文', summary='已发生', status='completed')
        prior_ch.summary_source_hash = summary_source_hash(prior_ch.title, prior_ch.content_text, prior_ch.summary)
        db.add(prior_ch)
        await db.commit()
        previous_id, current_id = previous.id, current.id
        monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', AsyncMock(return_value=SimpleNamespace(text='已发生')))
        await generate_volume_memory(db, previous_id, persist=True)
        memory = HierarchicalMemory(db)
        text = await memory._gather_volume_summaries(str(r.project), str(current_id))
        assert '已发生' in text and '结局' not in text
        assert await memory._gather_volume_summaries(str(uuid4()), str(current_id)) == ''
        assert await memory._gather_volume_summaries(str(r.project), str(uuid4())) == ''
