"""One fixed embedding configuration for an immutable retrieval index.

The space identifier contains no recoverable endpoint URL or credentials.
Queries and passages must use this same provider snapshot and dimension.
"""
from dataclasses import dataclass, field
import math

from app.services.entity_sources import digest

MAX_EMBEDDING_CHARS = 4000
EMBEDDING_BATCH_SIZE = 32


def ordered_embeddings(rows, count):
    """OpenAI-compatible batch indices bind every vector to its actual input."""
    if not isinstance(rows, (list, tuple)) or len(rows) != count:
        raise ValueError('embedding_response_count_invalid')
    ordered = [None] * count
    for row in rows:
        index = row.get('index') if isinstance(row, dict) else getattr(row, 'index', None)
        vector = row.get('embedding') if isinstance(row, dict) else getattr(row, 'embedding', None)
        if index is None and count == 1:
            index = 0
        if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
            raise ValueError('embedding_response_index_invalid')
        ordered[index] = validate_vector(vector)
    return ordered


def validate_vector(vector, dimension=None):
    if (not isinstance(vector, (list, tuple)) or not vector
            or (dimension is not None and len(vector) != dimension)
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
            or not any(v != 0 for v in vector)):
        raise ValueError('embedding_vector_invalid')
    return [float(v) for v in vector]


def space_identity(provider):
    """Fingerprint the logical encoder; key rotation does not change its space."""
    return digest(dict(version=1, provider=type(provider).__name__, model=provider.model,
        endpoint=provider.base_url, modality=getattr(provider, 'modality', None),
        encoding=getattr(provider, 'encoding_format', 'float'),
        truncate=getattr(provider, 'truncate', 'NONE')))


@dataclass(frozen=True)
class EmbeddingSpace:
    identity: str
    provider: object = field(repr=False)
    owns_provider: bool = False

    async def embed(self, texts, *, input_type):
        if input_type not in {'query', 'passage'} or not 1 <= len(texts) <= EMBEDDING_BATCH_SIZE:
            raise ValueError('embedding_batch_invalid')
        if any(not isinstance(t, str) or not t.strip() or len(t) > MAX_EMBEDDING_CHARS for t in texts):
            raise ValueError('embedding_input_invalid')
        if space_identity(self.provider) != self.identity:
            raise ValueError('embedding_configuration_changed')
        if getattr(self.provider, 'truncate', 'NONE') != 'NONE':
            raise ValueError('embedding_truncation_not_allowed')
        if getattr(self.provider, 'encoding_format', 'float') != 'float':
            raise ValueError('embedding_encoding_not_supported')
        vectors = await self.provider.embed_batch(texts, input_type=input_type)
        if len(vectors) != len(texts):
            raise ValueError('embedding_response_count_invalid')
        dimension = len(vectors[0]) if vectors else 0
        checked = [validate_vector(v, dimension) for v in vectors]
        if space_identity(self.provider) != self.identity:
            raise ValueError('embedding_configuration_changed')
        return checked

    async def aclose(self):
        """Only close this request's SDK client, never the router's shared one."""
        client = getattr(self.provider, '_client', None)
        if self.owns_provider and client is not None:
            await client.close()


async def current_embedding_space():
    from app.services.model_router import get_model_router_async, EmbeddingProvider, NvidiaEmbeddingProvider
    router = await get_model_router_async()
    provider = router.embedding_provider
    if provider is None or not callable(getattr(provider, 'embed_batch', None)):
        raise ValueError('embedding_not_configured')
    # Workers may run on successive event loops. Copy configuration into a
    # request-owned provider; no shared HTTP client/async lock is reused here.
    if isinstance(provider, NvidiaEmbeddingProvider):
        snapshot = NvidiaEmbeddingProvider(api_keys=list(provider.api_keys), base_url=provider.base_url,
            model=provider.model, modality=list(provider.modality), encoding_format=provider.encoding_format,
            truncate=provider.truncate, timeout=provider.timeout)
    elif isinstance(provider, EmbeddingProvider):
        snapshot = EmbeddingProvider(api_key=provider.api_key, base_url=provider.base_url, model=provider.model)
    else:
        raise ValueError('embedding_provider_unsupported')
    return EmbeddingSpace(space_identity(snapshot), snapshot, owns_provider=True)


def split_embedding_text(text, *, size=MAX_EMBEDDING_CHARS, overlap=120):
    """Cover the whole input; overlap keeps a phrase crossing a slice boundary."""
    if not isinstance(text, str) or not 0 <= overlap < size <= MAX_EMBEDDING_CHARS:
        raise ValueError('embedding_input_invalid')
    return [part for start in range(0, len(text), size - overlap)
            if (part := text[start:start + size]).strip()]
