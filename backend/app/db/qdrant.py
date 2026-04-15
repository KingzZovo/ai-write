"""Qdrant client wrapper."""

from collections.abc import AsyncGenerator

from qdrant_client import AsyncQdrantClient

from app.config import settings

_client: AsyncQdrantClient | None = None


async def init_qdrant() -> None:
    """Initialize the Qdrant async client."""
    global _client  # noqa: PLW0603
    _client = AsyncQdrantClient(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
    )


async def close_qdrant() -> None:
    """Close the Qdrant async client."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.close()
        _client = None


async def get_qdrant() -> AsyncGenerator[AsyncQdrantClient, None]:
    """FastAPI dependency that yields the Qdrant async client."""
    if _client is None:
        raise RuntimeError("Qdrant client has not been initialized")
    yield _client
