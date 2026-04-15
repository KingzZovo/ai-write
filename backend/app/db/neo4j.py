"""Neo4j async driver wrapper."""

from collections.abc import AsyncGenerator

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import settings

_driver: AsyncDriver | None = None


async def init_neo4j() -> None:
    """Initialize the Neo4j async driver."""
    global _driver  # noqa: PLW0603
    _driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )


async def close_neo4j() -> None:
    """Close the Neo4j async driver."""
    global _driver  # noqa: PLW0603
    if _driver is not None:
        await _driver.close()
        _driver = None


async def get_neo4j() -> AsyncGenerator[AsyncDriver, None]:
    """FastAPI dependency that yields the Neo4j async driver."""
    if _driver is None:
        raise RuntimeError("Neo4j driver has not been initialized")
    yield _driver
