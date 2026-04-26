"""v1.5.0 B1: tier-aware fallback chain unit tests."""
import asyncio
import pytest
from unittest.mock import AsyncMock
from app.services.model_router import (
    ModelRouter, GenerationResult, TokenUsage, BaseProvider,
)


class _StubProvider(BaseProvider):
    def __init__(self, *, fail: bool, tag: str):
        self.fail = fail
        self.tag = tag
        self.calls = 0

    async def generate(self, *, messages, model, temperature=None, max_tokens=None, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"stub-{self.tag}-fail")
        return GenerationResult(
            text=f"ok-{self.tag}",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            model=model, provider=self.tag,
        )

    async def generate_stream(self, *, messages, model, temperature=None, max_tokens=None, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"stub-{self.tag}-stream-fail")
        for piece in ["hel", "lo-", self.tag]:
            yield piece


def _make_router(*, flagship_fails: bool, standard_fails: bool = False) -> ModelRouter:
    r = ModelRouter.__new__(ModelRouter)
    r.providers = {
        "ep_flagship": _StubProvider(fail=flagship_fails, tag="flag"),
        "ep_standard": _StubProvider(fail=standard_fails, tag="std"),
    }
    r._endpoint_tiers = {"ep_flagship": "flagship", "ep_standard": "standard"}
    r._endpoint_defaults = {"ep_flagship": "flag-model", "ep_standard": "std-model"}
    r._task_routes = {}
    r._track = lambda usage: None
    return r


@pytest.mark.asyncio
async def test_build_attempts_route_then_chain():
    r = _make_router(flagship_fails=False)
    class Route: endpoint_id = "ep_flagship"; model = "flag-model"; temperature = 0.5; max_tokens = 1000
    attempts = r._build_tier_attempts(route=Route())
    # explicit endpoint first, then standard (skipping flagship), small filtered (no ep)
    assert [a[0] for a in attempts] == ["flagship", "standard"]
    assert attempts[0][1] == "ep_flagship"
    assert attempts[1][1] == "ep_standard"


@pytest.mark.asyncio
async def test_build_attempts_preferred_tier_only():
    r = _make_router(flagship_fails=False)
    # Stub _pick_endpoint_by_tier so it works without DB
    r._pick_endpoint_by_tier = lambda t: {"flagship": "ep_flagship", "standard": "ep_standard"}.get(t)
    attempts = r._build_tier_attempts(preferred_tier="flagship")
    assert [a[0] for a in attempts] == ["flagship", "standard"]


@pytest.mark.asyncio
async def test_generate_falls_back_on_first_failure():
    r = _make_router(flagship_fails=True, standard_fails=False)
    r._pick_endpoint_by_tier = lambda t: {"flagship": "ep_flagship", "standard": "ep_standard"}.get(t)
    class Route: endpoint_id = "ep_flagship"; model = "flag-model"; temperature = 0.5; max_tokens = 1000
    result = await r.generate_with_tier_fallback("generation", [{"role":"user","content":"hi"}], route=Route())
    assert result.text == "ok-std"
    assert r.providers["ep_flagship"].calls == 1
    assert r.providers["ep_standard"].calls == 1


@pytest.mark.asyncio
async def test_generate_raises_when_all_fail():
    r = _make_router(flagship_fails=True, standard_fails=True)
    r._pick_endpoint_by_tier = lambda t: {"flagship": "ep_flagship", "standard": "ep_standard"}.get(t)
    class Route: endpoint_id = "ep_flagship"; model = "flag-model"; temperature = 0.5; max_tokens = 1000
    with pytest.raises(RuntimeError, match="All tier-fallback attempts failed"):
        await r.generate_with_tier_fallback("generation", [{"role":"user","content":"hi"}], route=Route())


@pytest.mark.asyncio
async def test_stream_falls_back_before_first_chunk():
    r = _make_router(flagship_fails=True, standard_fails=False)
    r._pick_endpoint_by_tier = lambda t: {"flagship": "ep_flagship", "standard": "ep_standard"}.get(t)
    class Route: endpoint_id = "ep_flagship"; model = "flag-model"; temperature = 0.5; max_tokens = 1000
    chunks = []
    async for c in r.stream_with_tier_fallback("generation", [{"role":"user","content":"hi"}], route=Route()):
        chunks.append(c)
    assert "".join(chunks) == "hello-std"


@pytest.mark.asyncio
async def test_stream_no_endpoints_raises():
    r = ModelRouter.__new__(ModelRouter)
    r.providers = {}
    r._endpoint_tiers = {}
    r._endpoint_defaults = {}
    r._task_routes = {}
    r._track = lambda u: None
    r._pick_endpoint_by_tier = lambda t: None
    with pytest.raises(RuntimeError, match="No endpoints available"):
        async for _ in r.stream_with_tier_fallback("generation", [{"role":"user","content":"hi"}]):
            pass
