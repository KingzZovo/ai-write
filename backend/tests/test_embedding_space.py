"""Strict batch contracts and request-owned providers, without endpoint IO."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.embedding_space import (
    EMBEDDING_BATCH_SIZE, MAX_EMBEDDING_CHARS, EmbeddingSpace, current_embedding_space,
    ordered_embeddings, space_identity, split_embedding_text, validate_vector,
)
from app.services.model_router import EmbeddingProvider, NvidiaEmbeddingProvider


def test_batch_indices_bind_reordered_vectors_to_original_inputs():
    assert ordered_embeddings([NS(index=1, embedding=[0, 1]), NS(index=0, embedding=[1, 0])], 2) == [[1, 0], [0, 1]]
    assert ordered_embeddings([{'embedding': [2, 3]}], 1) == [[2, 3]]


@pytest.mark.parametrize('rows,count', [
    ([], 1), ([{'index': 0, 'embedding': [1]}], 2),
    ([{'index': 0, 'embedding': [1]}, {'index': 0, 'embedding': [2]}], 2),
    ([{'index': 0, 'embedding': [1]}, {'embedding': [2]}], 2),
    ([{'index': True, 'embedding': [1]}], 1),
    ([{'index': -1, 'embedding': [1]}], 1),
    ([{'index': 1, 'embedding': [1]}], 1),
    ([{'index': '0', 'embedding': [1]}], 1),
])
def test_malformed_batch_is_not_partially_accepted(rows, count):
    with pytest.raises(ValueError, match='embedding_response_'):
        ordered_embeddings(rows, count)


@pytest.mark.parametrize('vector', [None, [], [0, 0], [True, 1], ['1', 2], [float('nan')], [float('inf')]])
def test_invalid_vectors_cannot_certify_an_index_point(vector):
    with pytest.raises(ValueError, match='embedding_vector_invalid'):
        validate_vector(vector)


def test_vector_dimension_is_fixed():
    with pytest.raises(ValueError): validate_vector([1, 2], dimension=3)


def test_splitting_covers_whole_chinese_input_and_keeps_boundary_overlap():
    text = '沈青读了回执，却仍不能证实车是否坏了。' * 1500 + '末尾唯一句。'
    parts = split_embedding_text(text)
    assert len(parts) > 5 and all(len(part) <= MAX_EMBEDDING_CHARS for part in parts)
    assert parts[-1].endswith('末尾唯一句。')
    # Adjacent slices begin every 3880 characters, including the final tail.
    for i, part in enumerate(parts): assert part == text[i * 3880:i * 3880 + 4000]


def test_identity_changes_with_encoder_but_not_credentials():
    provider = EmbeddingProvider(api_key='test-key', base_url='https://encoder.invalid/v1', model='encoder-a')
    first = space_identity(provider)
    provider.api_key = 'rotated-test-key'
    assert space_identity(provider) == first
    assert 'encoder.invalid' not in first and 'test-key' not in first
    provider.model = 'encoder-b'
    assert space_identity(provider) != first
    assert 'rotated-test-key' not in repr(EmbeddingSpace(space_identity(provider), provider))


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_type', [EmbeddingProvider, NvidiaEmbeddingProvider])
async def test_provider_batch_does_not_clip_or_misorder_inputs(provider_type):
    provider = provider_type(api_key='test-key')
    texts = ['甲' * 3998 + '甲尾', '乙' * 3998 + '乙尾']
    rows = [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]
    if provider_type is EmbeddingProvider:
        request = AsyncMock(return_value=NS(data=rows))
        provider._client = NS(embeddings=NS(create=request))
    else:
        request = AsyncMock(return_value={'data': rows})
        provider._post = request
    assert await provider.embed_batch(texts, input_type='passage') == [[1, 0], [0, 1]]
    call = request.await_args
    assert (call.kwargs['input'] if provider_type is EmbeddingProvider else call.args[0]) == texts


@pytest.mark.asyncio
@pytest.mark.parametrize('texts,input_type', [([], 'query'), (['字'] * 33, 'passage'), ([' '], 'query'),
    (['字' * 4001], 'query'), (['内容'], 'unsupported')])
async def test_invalid_input_does_not_reach_provider(texts, input_type):
    provider = EmbeddingProvider(api_key='test-key')
    provider.embed_batch = AsyncMock()
    space = EmbeddingSpace(space_identity(provider), provider)
    with pytest.raises(ValueError): await space.embed(texts, input_type=input_type)
    provider.embed_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['model', 'dimension', 'count', 'truncate', 'encoding'])
async def test_batch_configuration_and_response_remain_consistent(mutation):
    provider = NvidiaEmbeddingProvider(api_key='test-key')
    async def response(texts, **kwargs):
        if mutation == 'model': provider.model = 'changed'
        if mutation == 'count': return [[1, 0]]
        return [[1, 0], [1, 0, 1] if mutation == 'dimension' else [0, 1]]
    provider.embed_batch = AsyncMock(side_effect=response)
    if mutation == 'truncate': provider.truncate = 'END'
    if mutation == 'encoding': provider.encoding_format = 'base64'
    space = EmbeddingSpace(space_identity(provider), provider)
    with pytest.raises(ValueError): await space.embed(['甲', '乙'], input_type='passage')
    if mutation in {'truncate', 'encoding'}: provider.embed_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_type', [EmbeddingProvider, NvidiaEmbeddingProvider])
async def test_current_space_owns_resources_and_does_not_close_shared_router(monkeypatch, provider_type):
    provider = provider_type(api_key='test-key')
    shared_client = NS(close=AsyncMock())
    provider._client = shared_client
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=NS(embedding_provider=provider)))
    space = await current_embedding_space()
    assert space.provider is not provider and space.identity == space_identity(provider)
    owned_client = NS(close=AsyncMock())
    space.provider._client = owned_client
    await space.aclose()
    owned_client.close.assert_awaited_once()
    shared_client.close.assert_not_awaited()
    if provider_type is NvidiaEmbeddingProvider: assert space.provider._key_lock is not provider._key_lock


@pytest.mark.asyncio
async def test_missing_embedding_configuration_has_no_zero_vector_fallback(monkeypatch):
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock(return_value=NS(embedding_provider=None)))
    with pytest.raises(ValueError, match='not_configured'): await current_embedding_space()
