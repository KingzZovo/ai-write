"""Run the pytest profile itself in child processes without external services."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def profile(pytester):
    backend = str(Path(__file__).resolve().parents[1])
    pytester.makeconftest(f"import sys\nsys.path.insert(0, {backend!r})\npytest_plugins = ['tests.support.offline']\n")
    return pytester


def test_external_markers_and_fixture_dependencies_only_skip_in_offline_mode(profile):
    profile.makepyfile("""
        import pytest
        @pytest.fixture
        def runtime_db(): return 'disposable fixture'
        @pytest.fixture
        def nested(runtime_db): return runtime_db
        def test_local(): assert 2 + 2 == 4
        @pytest.mark.external_services
        def test_explicit(): pass
        def test_inherited(nested): assert nested == 'disposable fixture'
    """)
    profile.runpytest_subprocess("--offline", "-q").assert_outcomes(passed=1, skipped=2)
    profile.runpytest_subprocess("-q").assert_outcomes(passed=3)


@pytest.mark.parametrize("swallow", [False, True])
@pytest.mark.parametrize("action", [
    "socket.create_connection(('example.invalid', 9))", "socket.socket()",
    "asyncio.run(asyncio.open_connection('example.invalid', 9))",
    "socket.getnameinfo(('192.0.2.1', 9), socket.NI_NAMEREQD)",
])
def test_connection_attempt_cannot_pass_even_if_application_swallows_it(profile, swallow, action):
    if swallow:
        action = "try:\n        " + action + "\n    except RuntimeError:\n        pass"
    profile.makepyfile("import socket, asyncio\ndef test_boundary():\n    " + action + "\n")
    result = profile.runpytest_subprocess("--offline", "-q")
    result.assert_outcomes(failed=1)
    assert "offline" in result.stdout.str()
    assert "PermissionError" not in result.stdout.str()


def test_swallowed_setup_and_teardown_connections_fail_the_respective_phase(profile):
    profile.makepyfile("""
        import socket, pytest
        def attempt():
            try: socket.getaddrinfo('example.invalid', 9)
            except RuntimeError: pass
        @pytest.fixture
        def bad_setup(): attempt()
        @pytest.fixture
        def bad_teardown():
            yield
            attempt()
        def test_setup(bad_setup): pass
        def test_teardown(bad_teardown): pass
    """)
    result = profile.runpytest_subprocess("--offline", "-q")
    result.assert_outcomes(passed=1, errors=2)
    assert "during setup" in result.stdout.str() and "during teardown" in result.stdout.str()


def test_import_time_connection_is_a_collection_error(profile):
    profile.makepyfile("""
        import socket
        try: socket.create_connection(('example.invalid', 9))
        except RuntimeError: pass
        def test_local(): pass
    """)
    result = profile.runpytest_subprocess("--offline", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "Offline collection attempted external I/O" in result.stderr.str()


def test_sqlite_and_asgi_stay_available(profile):
    profile.makepyfile("""
        import asyncio, sqlite3, httpx
        from fastapi import FastAPI
        def test_local_io():
            with sqlite3.connect(':memory:') as db:
                assert db.execute('select 7').fetchone() == (7,)
            app = FastAPI()
            @app.get('/ping')
            async def ping(): return {'ok': True}
            async def read():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    assert (await client.get('/ping')).json() == {'ok': True}
            asyncio.run(read())
    """)
    profile.runpytest_subprocess("--offline", "-q").assert_outcomes(passed=1)
