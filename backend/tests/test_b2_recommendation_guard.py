"""v1.5.0 B2: prompt recommendation mismatch soft-guard regression tests.

White-box tests against the helper + a router-level smoke test using FastAPI's
in-process AsyncClient (already wired via tests/conftest.py).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.api.prompts import (
    _check_recommendation_mismatch,
    _endpoint_kind_from_tier,
)


class _StubEndpoint:
    def __init__(self, *, ep_id: str, name: str, tier: str | None) -> None:
        self.id = ep_id
        self.name = name
        self.tier = tier


def _make_db(*, endpoint: _StubEndpoint | None) -> object:
    db = AsyncMock()
    db.get = AsyncMock(return_value=endpoint)
    return db


def test_endpoint_kind_from_tier_maps_embedding():
    assert _endpoint_kind_from_tier("embedding") == "embedding"
    assert _endpoint_kind_from_tier("EMBEDDING") == "embedding"
    assert _endpoint_kind_from_tier("standard") == "chat"
    assert _endpoint_kind_from_tier("flagship") == "chat"
    assert _endpoint_kind_from_tier(None) == "chat"
    assert _endpoint_kind_from_tier("") == "chat"


@pytest.mark.asyncio
async def test_no_mismatch_when_endpoint_id_none():
    """Unbound prompt: no warning regardless of task_type."""
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=None),
        task_type="generation",
        endpoint_id=None,
        model_tier=None,
    )
    assert out is None


@pytest.mark.asyncio
async def test_no_mismatch_when_task_type_unknown():
    """Unknown task_type: skip warning (default rec is just a guess)."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="emb", tier="embedding")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="task_that_does_not_exist",
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is None


@pytest.mark.asyncio
async def test_no_mismatch_when_kinds_and_tiers_align():
    """generation prompt bound to flagship chat endpoint: no warning."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="flag-ep", tier="flagship")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="generation",  # rec: chat/flagship
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is None


@pytest.mark.asyncio
async def test_kind_mismatch_chat_prompt_on_embedding_endpoint():
    """v1.4.x 401 incident root cause: generation prompt -> embedding endpoint."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="emb-ep", tier="embedding")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="generation",  # rec: chat/flagship
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is not None
    assert out["code"] == "recommendation_mismatch"
    assert out["recommended_kind"] == "chat"
    assert out["recommended_tier"] == "flagship"
    assert out["current_kind"] == "embedding"
    assert out["current_tier"] == "embedding"
    assert out["kind_mismatch"] is True
    assert out["tier_mismatch"] is True
    assert out["endpoint_name"] == "emb-ep"


@pytest.mark.asyncio
async def test_tier_mismatch_only_generation_on_standard():
    """generation prompt bound to standard endpoint: tier-only mismatch warning."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="std-ep", tier="standard")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="generation",  # rec: chat/flagship
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is not None
    assert out["recommended_tier"] == "flagship"
    assert out["current_tier"] == "standard"
    assert out["kind_mismatch"] is False
    assert out["tier_mismatch"] is True


@pytest.mark.asyncio
async def test_model_tier_override_resolves_mismatch():
    """If the operator pins model_tier=flagship even on a standard endpoint, the
    effective tier is flagship and the warning disappears."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="std-ep", tier="standard")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="generation",
        endpoint_id=ep_id,
        model_tier="flagship",
    )
    assert out is None


@pytest.mark.asyncio
async def test_polishing_on_flagship_is_tier_mismatch():
    """polishing rec is standard; binding to flagship yields tier mismatch."""
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="flag-ep", tier="flagship")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="polishing",  # rec: chat/standard
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is not None
    assert out["recommended_tier"] == "standard"
    assert out["current_tier"] == "flagship"
    assert out["kind_mismatch"] is False
    assert out["tier_mismatch"] is True


@pytest.mark.asyncio
async def test_embedding_prompt_on_chat_endpoint_kind_mismatch():
    ep_id = uuid.uuid4()
    ep = _StubEndpoint(ep_id=str(ep_id), name="std-ep", tier="standard")
    out = await _check_recommendation_mismatch(
        db=_make_db(endpoint=ep),
        task_type="embedding",  # rec: embedding/embedding
        endpoint_id=ep_id,
        model_tier=None,
    )
    assert out is not None
    assert out["recommended_kind"] == "embedding"
    assert out["current_kind"] == "chat"
    assert out["kind_mismatch"] is True
