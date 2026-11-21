"""Preserve PostgreSQL UUID identity in SQLite test substitutes.

SQLite gives a literal UUID column NUMERIC affinity. Hex strings containing
only digits (or an exponent-looking 'e') then lose precision or become inf.
CHAR(32) keeps their exact representation without changing PostgreSQL DDL.
"""
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles, deregister


def pytest_configure(config):
    @compiles(UUID, 'sqlite')
    def compile_sqlite_uuid(element, compiler, **kwargs):
        return 'CHAR(32)'


def pytest_unconfigure(config):
    deregister(UUID)
