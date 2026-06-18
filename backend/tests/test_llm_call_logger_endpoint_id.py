"""Regression: llm_call_logger must not poison the DB session with a
non-UUID endpoint_id.

When model_router falls back to the env-based provider, endpoint_id is a
plain string ("env_openai"). The endpoint_id column is a UUID FK; passing the
string to asyncpg raised DataError, aborting the whole transaction and closing
the connection (observed cascading into Transaction.rollback() InterfaceError
on the 神裔 full-flow run). _coerce_uuid_or_none must map any non-UUID id to
None before the row is built.
"""
from __future__ import annotations

import uuid

from app.services.llm_call_logger import _coerce_uuid_or_none


def test_non_uuid_endpoint_id_coerced_to_none():
    assert _coerce_uuid_or_none("env_openai") is None


def test_none_endpoint_id_stays_none():
    assert _coerce_uuid_or_none(None) is None


def test_valid_uuid_string_preserved():
    u = uuid.uuid4()
    assert _coerce_uuid_or_none(str(u)) == u


def test_uuid_object_preserved():
    u = uuid.uuid4()
    assert _coerce_uuid_or_none(u) == u
