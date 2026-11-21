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

def __getattr__(name: str):
    """兼容旧的 ``neo4j._driver`` 直读。

    调用方一律是这个模式:``await init_neo4j(); driver = neo4j._driver`` +
    ``if driver is None: raise RuntimeError('graph_unavailable')``。驱动改为按
    event loop 缓存后,模块级 ``_driver`` 名字消失,这些调用方会拿到
    AttributeError(实测 entity_tasks / outline_settings_delivery /
    outline_to_facts)。这里把 ``_driver`` 映射为「当前 running loop 的驱动」,
    与旧语义一致:init 之后非 None,否则 None。
    """
    if name == "_driver":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return _drivers.get(id(loop))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
