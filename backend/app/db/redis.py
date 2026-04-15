"""Redis client wrapper."""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.config import settings

_client: Redis | None = None  # type: ignore[type-arg]


async def init_redis() -> None:
    """Initialize the Redis async client."""
    global _client  # noqa: PLW0603
    _client = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )


async def close_redis() -> None:
    """Close the Redis async client."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.close()
        _client = None


async def get_redis() -> AsyncGenerator[Redis, None]:  # type: ignore[type-arg]
    """FastAPI dependency that yields the Redis async client."""
    if _client is None:
        raise RuntimeError("Redis client has not been initialized")
    yield _client
