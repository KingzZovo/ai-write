"""Regression tests for the per-event-loop Neo4j driver lifecycle.

Locks the P2-7 fix in ``app/db/neo4j.py``: ``init_neo4j()`` used to close
whatever driver the current loop already had. On the request path that driver
is still held by a concurrent caller on the *same* loop, so closing it produced
``DriverError: Driver closed`` and a misleading 503 ("当前世界规则暂时无法读取").
These assertions are written against ``_new_driver`` / ``close`` so they go red
against the old close-on-init implementation.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import neo4j as neo4j_mod


def _fake_driver():
    driver = MagicMock(name="neo4j-driver")
    driver.close = AsyncMock()
    return driver


def _isolate(monkeypatch):
    created = []

    def fake_new_driver():
        driver = _fake_driver()
        created.append(driver)
        return driver

    # Keep the real per-loop cache and the module-level mirror untouched.
    monkeypatch.setattr(neo4j_mod, "_new_driver", fake_new_driver)
    monkeypatch.setattr(neo4j_mod, "_drivers", {})
    monkeypatch.setattr(neo4j_mod, "_driver", None)
    return created


@pytest.mark.asyncio
async def test_init_neo4j_never_closes_a_driver_another_caller_still_holds(monkeypatch):
    created = _isolate(monkeypatch)

    # Caller A initializes on this loop and keeps using the driver it received.
    await neo4j_mod.init_neo4j()
    # Caller B initializes on the *same* loop, exactly like a second request
    # hitting app.api.outlines / outline_settings_delivery / outline_to_facts.
    await neo4j_mod.init_neo4j()

    assert len(created) == 1, "init must reuse the loop's driver, not build a new one"
    assert created[0].close.await_count == 0, (
        "init closed a driver a concurrent caller on the same loop still holds (P2-7)"
    )


@pytest.mark.asyncio
async def test_close_drops_the_entry_so_a_later_init_builds_a_fresh_driver(monkeypatch):
    created = _isolate(monkeypatch)

    await neo4j_mod.init_neo4j()
    await neo4j_mod.close_neo4j()
    assert created[0].close.await_count == 1
    assert neo4j_mod._driver is None

    # Nothing may hand back the closed driver for this loop afterwards (P2-6:
    # a dead driver must never be reused).
    await neo4j_mod.init_neo4j()
    assert len(created) == 2 and created[1] is not created[0]