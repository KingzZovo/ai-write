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

_drivers: dict[asyncio.AbstractEventLoop, AsyncDriver] = {}

# Mirror of "the driver for the loop that most recently asked for one". Callers
# use the ``await init_neo4j(); driver = neo4j._driver`` pattern, so this stays
# readable — but unlike the module-level ``__getattr__`` it replaces, it is a
# *plain module attribute*. pytest's ``monkeypatch.setattr(neo4j, '_driver',
# fake)`` captures ``oldval = getattr(...)`` when patching and writes that value
# back on undo, so a real attribute restores the true previous value instead of
# pinning a stale driver into the module dict forever.
_driver: AsyncDriver | None = None


def _new_driver() -> AsyncDriver:
    return AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )


def _driver_for_running_loop() -> AsyncDriver:
    """Return the driver bound to the current loop, creating and caching it if needed."""
    global _driver
    loop = asyncio.get_running_loop()
    driver = _drivers.get(loop)
    if driver is None:
        driver = _new_driver()
        _drivers[loop] = driver
    _driver = driver
    return driver


async def init_neo4j() -> None:
    """Ensure the current event loop has a driver; never close a live one.

    Idempotent on purpose. Closing whatever driver the loop already had would
    kill the driver a concurrent request on the same loop was still using —
    observed as ``DriverError: Driver closed``, which ``read_world_rule_source``
    re-wrapped as a misleading 503 "当前世界规则暂时无法读取". Replacing a driver
    is explicit: see :func:`reset_neo4j`.
    """
    _driver_for_running_loop()


async def reset_neo4j() -> None:
    """Close and replace the current loop's driver (explicit reconnect)."""
    global _driver
    loop = asyncio.get_running_loop()
    previous = _drivers.get(loop)
    driver = _new_driver()
    _drivers[loop] = driver
    _driver = driver
    if previous is not None and previous is not driver:
        try:
            await previous.close()
        except Exception:  # noqa: BLE001 — replacing a stale driver must not fail the caller
            pass


async def close_neo4j() -> None:
    """Close the driver bound to the current event loop and drop its cache entry."""
    global _driver
    loop = asyncio.get_running_loop()
    driver = _drivers.pop(loop, None)
    if driver is not None and _driver is driver:
        _driver = None
    if driver is not None:
        await driver.close()


async def get_neo4j() -> AsyncGenerator[AsyncDriver, None]:
    """FastAPI dependency that yields the Neo4j async driver for the current loop.

    Lazily creates one when missing: celery prefork children never run the
    FastAPI lifespan, so requiring a prior ``init_neo4j()`` made the whole
    writing chain fail there.
    """
    yield _driver_for_running_loop()
