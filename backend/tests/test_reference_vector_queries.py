from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.services.qdrant_store import QdrantStore


async def seed(client, collection):
    await client.create_collection(collection, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    await client.upsert(collection, [
        PointStruct(id=1, vector=[1.0, 0.0], payload={'book_id': 'other', 'marker': '异书'}),
        PointStruct(id=2, vector=[0.9, 0.1], payload={'book_id': 'bound', 'marker': '匹配'}),
        PointStruct(id=3, vector=[0.6, 0.4], payload={'book_id': 'bound', 'marker': '次选'}),
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize('collection,method', [
    ('plots', 'search_similar_plots'), ('styles', 'search_similar_styles'),
    ('style_profiles', 'search_style_profiles'), ('beat_sheets', 'search_beat_sheets'),
    ('style_samples_redacted', 'search_style_samples_redacted'),
])
async def test_current_client_reference_search_keeps_book_filter_and_ranking(collection, method):
    client = AsyncQdrantClient(':memory:')
    try:
        await seed(client, collection)
        hits = await getattr(QdrantStore(client), method)([1.0, 0.0], book_id='bound', top_k=1)
        assert len(hits) == 1
        assert hits[0]['payload']['marker'] == '匹配'
        assert hits[0]['score'] > 0.9
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_generic_and_scene_search_use_current_api_and_preserve_scope():
    client = AsyncQdrantClient(':memory:')
    try:
        await seed(client, 'custom')
        hits = await QdrantStore(client).search_by_vector('custom', [1.0, 0.0], {'book_id': 'bound'}, top_k=1)
        assert [h['id'] for h in hits] == [2]
        name = QdrantStore.SCENE_SAMPLES_COLLECTION
        await client.create_collection(name, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        await client.upsert(name, [
            PointStruct(id=1, vector=[1.0, 0.0], payload={'profile_id': 'other', 'scene_type': '冲突'}),
            PointStruct(id=2, vector=[0.9, 0.1], payload={'profile_id': 'selected', 'scene_type': '冲突'}),
            PointStruct(id=3, vector=[1.0, 0.0], payload={'profile_id': 'selected', 'scene_type': '抒情'}),
        ])
        hits = await QdrantStore(client).search_scene_samples([1.0, 0.0], profile_id='selected', scene_type='冲突')
        assert [h['id'] for h in hits] == [2]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_client_is_still_supported():
    class Legacy:
        async def search(self, **kwargs):
            assert kwargs['query_vector'] == [1.0, 0.0]
            assert kwargs['query_filter'].must[0].match.value == 'bound'
            return [SimpleNamespace(id=2, score=0.9, payload={'book_id': 'bound'})]
    hits = await QdrantStore(Legacy()).search_style_profiles([1.0, 0.0], book_id='bound')
    assert hits == [{'id': 2, 'score': 0.9, 'payload': {'book_id': 'bound'}}]


@pytest.mark.asyncio
async def test_reference_vectors_reach_context_without_other_book_content(monkeypatch):
    from app.services.context_pack import ContextPack, ContextPackBuilder
    client = AsyncQdrantClient(':memory:')
    db = AsyncMock()
    db.get.return_value = None  # No consolidated dossier; vector fallback is required.
    monkeypatch.setattr('app.services.style_runtime.resolve_reference_book_id', AsyncMock(return_value='bound'))
    try:
        for collection, field, value in [
            ('style_profiles', 'profile', {'pov': '限知视角', 'sentence_rhythm': '长短交替', 'emotional_register': '克制', 'vocab_tone': ['口语']}),
            ('beat_sheets', 'beat', {'scene_type': '对峙', 'reusable_pattern': '试探后提出条件', 'outcome': '关系变化'}),
        ]:
            await client.create_collection(collection, vectors_config=VectorParams(size=2, distance=Distance.COSINE))
            await client.upsert(collection, [
                PointStruct(id=1, vector=[1.0, 0.0], payload={'book_id': 'other', field: {'pov': '异书污染', 'scene_type': '异书污染'}}),
                PointStruct(id=2, vector=[0.9, 0.1], payload={'book_id': 'bound', field: value}),
            ])
        pack = ContextPack()
        await ContextPackBuilder(db=db)._v2_three_way_recall(pack, [1.0, 0.0], client, 'project')
        text = '\n'.join(pack.style_recall)
        assert '限知视角' in text and '试探后提出条件' in text
        assert '异书污染' not in text
        assert pack.rag_snippets == []
        assert '限知视角' in pack.to_system_prompt()
    finally:
        await client.close()
