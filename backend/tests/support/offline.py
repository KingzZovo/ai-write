"""Explicit offline pytest profile; never substitutes for integration tests.

The socket guard is an accidental-I/O check within this Python process, not
an OS sandbox. External worker/subprocess tests must be marked before running.
"""
from __future__ import annotations

import asyncio
import socket
import traceback
from dataclasses import dataclass, field

import pytest


class OfflineNetworkError(RuntimeError):
    """A test attempted external I/O under the offline profile."""


@dataclass
class NetworkGuard:
    patcher: pytest.MonkeyPatch = field(default_factory=pytest.MonkeyPatch)
    attempts: list[str] = field(default_factory=list)

    def install(self) -> None:
        original_init = socket.socket.__init__

        def forbidden(*_args, **_kwargs):
            # Do not retain addresses, URLs, credentials, or query payloads.
            origin = " -> ".join(
                f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} ({frame.name})"
                for frame in traceback.extract_stack()
                if "/backend/" in frame.filename and frame.filename != __file__
            )
            self.attempts.append(origin or "socket I/O")
            raise OfflineNetworkError("External I/O is disabled by pytest --offline")

        async def forbidden_async(*_args, **_kwargs):
            forbidden()

        def guarded_init(sock, family=-1, type=-1, proto=-1, fileno=None):
            # asyncio needs a local socketpair for its wakeup pipe. Block
            # Internet sockets before the OS call, including socket()'s
            # implicit AF_INET default, so caught permission errors cannot
            # hide unmocked network access in restricted environments.
            if family != socket.AF_UNIX and fileno is None:
                forbidden()
            original_init(sock, family, type, proto, fileno)

        self.patcher.setattr(socket.socket, "__init__", guarded_init)
        # Refuse asynchronous DNS/connection work before asyncio dispatches
        # DNS into its default executor. No worker thread is needed to fail.
        for name in ("getaddrinfo", "getnameinfo", "create_connection", "create_server", "create_datagram_endpoint"):
            self.patcher.setattr(asyncio.BaseEventLoop, name, forbidden_async)
        for name in ("connect", "connect_ex", "sendto", "bind", "listen"):
            self.patcher.setattr(socket.socket, name, forbidden)
        for name in ("create_connection", "getaddrinfo", "getnameinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            self.patcher.setattr(socket, name, forbidden)


GUARD = pytest.StashKey[NetworkGuard]()


def pytest_addoption(parser):
    parser.addoption("--offline", action="store_true", help="Skip external-service tests and reject socket I/O")


def pytest_configure(config):
    config.addinivalue_line("markers", "external_services: requires disposable database, broker, graph, or network services")
    if config.getoption("offline"):
        guard = NetworkGuard()
        config.stash[GUARD] = guard
        guard.install()


def pytest_collection_modifyitems(config, items):
    for item in items:
        # All derived fixtures retain this shared, explicitly disposable PG
        # fixture in their closure, even when it is imported through modules.
        if "runtime_db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.external_services)
        if config.getoption("offline") and item.get_closest_marker("external_services"):
            item.add_marker(pytest.mark.skip(reason="External-service integration test excluded by --offline"))


def pytest_collection_finish(session):
    guard = session.config.stash.get(GUARD, None)
    if guard is not None and guard.attempts:
        raise pytest.UsageError("Offline collection attempted external I/O; remove import-time connections")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    guard = item.config.stash.get(GUARD, None)
    if guard is None or not guard.attempts:
        return
    count = len(guard.attempts)
    origins = "; ".join(dict.fromkeys(guard.attempts))
    guard.attempts.clear()
    report = outcome.get_result()
    # A service may catch RuntimeError and return empty data. Such a test
    # still fails; a swallowed connection attempt cannot count as coverage.
    if report.outcome != "failed":
        report.outcome = "failed"
        report.longrepr = (f"pytest --offline blocked {count} external I/O attempt(s) during {call.when}. "
                           f"Mock the boundary or mark the test external_services. Origin: {origins}")


def pytest_unconfigure(config):
    guard = config.stash.get(GUARD, None)
    if guard is not None:
        guard.patcher.undo()
