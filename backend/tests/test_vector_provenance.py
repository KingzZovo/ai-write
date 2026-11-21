import hashlib
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Volume
from app.services.summary_provenance import summary_source_hash
from app.services.vector_provenance import filter_current_memory, summary_receipt
from app.services.chapter_chunker import chunk_chapter_text
from app.services import qdrant_store as qs


async def prepare(r):
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        ch.content_text = '人物在岸边等船。\n\n船家收起缆绳。' * 40
        ch.summary = '人物等船，船家收绳。'
        ch.status = 'completed'
        ch.global_idx = 2
        ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
        await db.commit()
        payload = dict(project_id=str(r.project), chapter_id=str(ch.id), volume_id=str(ch.volume_id),
                       chapter_idx=ch.chapter_idx, global_idx=ch.global_idx, summary=ch.summary,
                       summary_source_hash=ch.summary_source_hash)
        chunk = dict(payload, chunk_seq=0, text=chunk_chapter_text(ch.content_text)[0],
                     source_content_hash=hashlib.sha256(ch.content_text.encode()).hexdigest())
        return payload, chunk


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'body', 'summary', 'draft', 'reorder', 'delete', 'foreign', 'legacy'])
async def test_current_database_fences_summary_and_chunk_vectors(runtime_db, change):
    r = runtime_db
    payload, chunk = await prepare(r)
    async with r.sessions() as stale:
        await stale.get(Chapter, r.chapter)
        async with r.sessions() as author:
            values = {'body': {'content_text': '作者新正文。'}, 'summary': {'summary': '另一个摘要'},
                      'draft': {'status': 'needs_review'}, 'reorder': {'global_idx': 22}}
            if change in values:
                await author.execute(update(Chapter).where(Chapter.id == r.chapter).values(**values[change]))
            if change == 'delete':
                from app.models.pipeline import PipelineChapterStatus
                await author.execute(delete(PipelineChapterStatus).where(PipelineChapterStatus.chapter_id == r.chapter))
                await author.execute(delete(Chapter).where(Chapter.id == r.chapter))
            await author.commit()
        if change == 'foreign':
            payload['project_id'] = chunk['project_id'] = 'other'
        if change == 'legacy':
            payload.pop('summary_source_hash')
            chunk.pop('source_content_hash')
        summaries = await filter_current_memory(stale, r.project, [{'payload': payload}], before_global_idx=10)
        chunks = await filter_current_memory(stale, r.project, [{'payload': chunk}], before_global_idx=10, chunks=True)
        assert bool(summaries) == (change == 'none')
        assert bool(chunks) == (change in {'none', 'summary'})


@pytest.mark.asyncio
async def test_compacted_receipt_and_chunk_text_tampering_are_rejected(runtime_db):
    r = runtime_db
    payload, chunk = await prepare(r)
    compact = {'tier': 'compacted', 'payload': {'project_id': str(r.project), 'summary': '合并摘要', 'source_count': 1,
        'source_global_idx_min': 2, 'source_global_idx_max': 2, 'sources': [summary_receipt(payload)]}}
    async with r.sessions() as db:
        assert await filter_current_memory(db, r.project, [compact], before_global_idx=10) == [compact]
        for mutate in ['range', 'count', 'hash', 'missing']:
            bad = deepcopy(compact)
            if mutate == 'range': bad['payload']['source_global_idx_max'] = 3
            if mutate == 'count': bad['payload']['source_count'] = 2
            if mutate == 'hash': bad['payload']['sources'][0]['summary_source_hash'] = 'wrong'
            if mutate == 'missing': bad['payload'].pop('sources')
            assert await filter_current_memory(db, r.project, [bad], before_global_idx=10) == []
        chunk['text'] = '并非原文的片段'
        assert await filter_current_memory(db, r.project, [{'payload': chunk}], before_global_idx=10, chunks=True) == []
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='来源已改稿'))
        await db.commit()
        assert await filter_current_memory(db, r.project, [compact], before_global_idx=10) == []


@pytest.mark.asyncio
async def test_real_qdrant_recall_cannot_bypass_current_source_check(runtime_db, monkeypatch):
    from app.services.context_pack import ContextPack, ContextPackBuilder
    r = runtime_db
    payload, chunk = await prepare(r)
    client = AsyncQdrantClient(':memory:')
    real_close = client.close
    monkeypatch.setattr(client, 'close', AsyncMock())
    monkeypatch.setattr('qdrant_client.AsyncQdrantClient', lambda **kw: client)
    monkeypatch.setattr('app.services.feature_extractor.generate_embedding', AsyncMock(return_value=[1.0, 0.0]))
    try:
        from app.services.chapter_summarizer import upsert_chapter_summary_embedding
        from app.services.chapter_chunker import upsert_chapter_chunks
        monkeypatch.setattr('app.services.chapter_summarizer._maybe_trigger_auto_compaction', AsyncMock())
        async with r.sessions() as source_db:
            chapter = await source_db.get(Chapter, r.chapter)
            assert await upsert_chapter_summary_embedding(
                project_id=r.project, volume_id=chapter.volume_id, chapter_id=chapter.id,
                chapter_idx=chapter.chapter_idx, global_idx=chapter.global_idx,
                chapter_title=chapter.title, summary=chapter.summary,
                source_hash=chapter.summary_source_hash,
            )
            assert await upsert_chapter_chunks(
                project_id=r.project, volume_id=chapter.volume_id, chapter_id=chapter.id,
                chapter_idx=chapter.chapter_idx, global_idx=chapter.global_idx,
                content_text=chapter.content_text,
            ) > 0
        # Stale nearest neighbours must not crowd a lower-ranked valid source
        # out of the generation window before provenance filtering.
        live = qs.chapter_summaries_collection(r.project)
        stored, _ = await client.scroll(live, with_payload=True)
        await client.upsert(live, [PointStruct(id=stored[0].id, vector=[0.9, 0.1], payload=stored[0].payload)])
        await client.upsert(live, [PointStruct(id=100 + i, vector=[1.0, 0.0],
            payload=dict(payload, summary_source_hash='stale')) for i in range(7)])
        async with r.sessions() as db:
            builder = ContextPackBuilder(db=db)
            pack = ContextPack(history_before_global_idx=10)
            await builder._search_qdrant_snippets(pack, ['船'], str(r.project))
            assert pack.rag_snippets == [payload['summary']]
            assert pack.chunk_recall
            await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='作者已改稿。'))
            await db.commit()
            pack = ContextPack(history_before_global_idx=10)
            await builder._search_qdrant_snippets(pack, ['船'], str(r.project))
            assert pack.rag_snippets == [] and pack.chunk_recall == []
    finally:
        await real_close()


@pytest.mark.asyncio
async def test_compaction_stores_verified_receipts_and_invalidates_on_source_change(runtime_db, monkeypatch):
    from app.services import memory_compactor as mc
    r = runtime_db
    first, _ = await prepare(r)
    payloads = [first]
    async with r.sessions() as db:
        for idx in range(3, 8):
            ch = Chapter(volume_id=first['volume_id'], chapter_idx=idx, global_idx=idx,
                         title=f'第{idx}章', content_text=f'事件{idx}。', summary=f'摘要{idx}。', status='completed')
            ch.summary_source_hash = summary_source_hash(ch.title, ch.content_text, ch.summary)
            db.add(ch)
            await db.flush()
            payloads.append(dict(project_id=str(r.project), chapter_id=str(ch.id), volume_id=str(ch.volume_id),
                                 chapter_idx=idx, global_idx=idx, summary=ch.summary, summary_source_hash=ch.summary_source_hash))
        await db.commit()
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text='这一章已改稿'))
        await db.commit()
    client = AsyncQdrantClient(':memory:')
    real_close = client.close
    monkeypatch.setattr(client, 'close', AsyncMock())
    monkeypatch.setattr(mc, 'AsyncQdrantClient', lambda **kw: client)
    monkeypatch.setattr(mc, 'run_structured_prompt', AsyncMock(return_value={'summary': '合并有效来源。'}))
    monkeypatch.setattr(mc, 'generate_embedding', AsyncMock(return_value=[1.0, 0.0]))
    try:
        live = qs.chapter_summaries_collection(r.project)
        await client.create_collection(live, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        await client.upsert(live, [PointStruct(id=i, vector=[1.0, 0.0], payload=p) for i, p in enumerate(payloads)])
        async with r.sessions() as db:
            result = await mc.compact_project_memory(project_id=str(r.project), db=db, force=True)
            assert result['rejected_sources'] == 1
            assert result['compacted'] == 4
            points, _ = await client.scroll(qs.compacted_summaries_collection(r.project), with_payload=True)
            assert len(points) == 1
            compact = {'tier': 'compacted', 'payload': points[0].payload}
            assert compact['payload']['source_count'] == len(compact['payload']['sources']) == 4
            assert await filter_current_memory(db, r.project, [compact], before_global_idx=10) == [compact]
            source_id = compact['payload']['sources'][0]['chapter_id']
            await db.execute(update(Chapter).where(Chapter.id == source_id).values(status='needs_review'))
            await db.commit()
            assert await filter_current_memory(db, r.project, [compact], before_global_idx=10) == []
    finally:
        await real_close()


@pytest.mark.asyncio
async def test_agent_memory_tool_uses_trusted_target_and_closes_client(runtime_db, monkeypatch):
    from uuid import uuid4
    from app.services.tool_registry import run_tool
    r = runtime_db
    payload, _ = await prepare(r)
    async with r.sessions() as db:
        target = Chapter(volume_id=payload['volume_id'], chapter_idx=10, global_idx=10, title='写作目标')
        db.add(target)
        await db.commit()
        target_id = str(target.id)
    client = AsyncQdrantClient(':memory:')
    real_close = client.close
    close = AsyncMock()
    monkeypatch.setattr(client, 'close', close)
    monkeypatch.setattr('qdrant_client.AsyncQdrantClient', lambda **kw: client)
    monkeypatch.setattr('app.services.feature_extractor.generate_embedding', AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr('app.services.llm_call_logger.log_llm_call', AsyncMock())
    try:
        await client.create_collection(qs.chapter_summaries_collection(r.project), vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        await client.upsert(qs.chapter_summaries_collection(r.project), [PointStruct(id=1, vector=[1.0, 0.0], payload=payload)])
        model_args = {'query': '船', 'project_id': str(uuid4()), 'chapter_id': str(uuid4()), 'top_k': 10000}
        async with r.sessions() as db:
            result = await run_tool('search_memory', model_args, db, project_id=str(r.project), chapter_id=target_id)
            assert result == {'snippets': [payload['summary']]}
            close.assert_awaited_once()
            assert await run_tool('search_memory', model_args, db, project_id=str(r.project)) == {'snippets': []}
            assert 'error' in await run_tool('search_memory', model_args, db)
            await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(status='needs_review'))
            await db.commit()
            assert await run_tool('search_memory', model_args, db, project_id=str(r.project), chapter_id=target_id) == {'snippets': []}
    finally:
        await real_close()
