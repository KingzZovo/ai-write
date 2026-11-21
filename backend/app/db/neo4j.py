"""Neo4j async driver wrapper.

A driver is bound to the event loop that created it. Sharing one global driver
across loops breaks in celery prefork workers (which build a fresh loop per
task) with ``got Future attached to a different loop`` — observed as every
chapter failing with a vague ``world_rules_context_unavailable``. Keep one
driver per running loop instead.
"""

from collections.abc import AsyncGenerator
import asyncio

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import settings

_drivers: dict[int, AsyncDriver] = {}


def _new_driver() -> AsyncDriver:
    return AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )


async def init_neo4j() -> None:
    """Initialize the Neo4j async driver for the current event loop."""
    loop = asyncio.get_running_loop()
    previous = _drivers.get(id(loop))
    _drivers[id(loop)] = _new_driver()
    if previous is not None:
        try:
            await previous.close()
        except Exception:  # noqa: BLE001 — replacing a stale driver must not fail startup
            pass


async def close_neo4j() -> None:
    """Close the Neo4j async driver for the current event loop."""
    loop = asyncio.get_running_loop()
    driver = _drivers.pop(id(loop), None)
    if driver is not None:
        await driver.close()


async def get_neo4j() -> AsyncGenerator[AsyncDriver, None]:
    """FastAPI dependency that yields the Neo4j async driver.

    Lazily creates one when missing: celery prefork children never run the
    FastAPI lifespan, so requiring a prior ``init_neo4j()`` made the whole
    writing chain fail there.
    """
    loop = asyncio.get_running_loop()
    driver = _drivers.get(id(loop))
    if driver is None:
        driver = _new_driver()
        _drivers[id(loop)] = driver
    yield driver
