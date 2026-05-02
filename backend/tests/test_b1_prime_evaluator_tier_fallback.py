"""v1.5.0 B1': evaluator + checker LLM call sites use tier-aware fallback.

Guards against regression of the P0 bug where ChapterEvaluator silently used
sync ``get_model_router()`` inside an async handler -> unloaded router ->
'No model configured for evaluation' on every call. Also covers the matching
fix in consistency_checker._llm_consistency_check and ooc_checker._llm_ooc_check,
and the v1.5 fallback-chain enhancements (_pick_endpoints_by_tier + safety net).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
        if self.fail:
            raise RuntimeError(f"stub-{self.tag}-stream-fail")
        for piece in ("x", "y", self.tag):
            yield piece


def _make_router(eps: dict[str, tuple[str, bool]]) -> ModelRouter:
    """eps: {key: (tier, fail)}."""
    r = ModelRouter.__new__(ModelRouter)
    r.providers = {k: _StubProvider(fail=fail, tag=k) for k, (_, fail) in eps.items()}
    r._endpoint_tiers = {k: tier for k, (tier, _) in eps.items()}
    r._endpoint_defaults = {k: f"{k}-model" for k in eps}
    r._task_routes = {}
    r._track = lambda usage: None
    return r


# ===========================================================================
# Fallback-chain enhancement tests (v1.5 B1')
# ===========================================================================

def test_pick_endpoints_by_tier_enumerates_all():
    """_pick_endpoints_by_tier returns ALL endpoints in tier (not just first)."""
    r = _make_router({
        "ep_a": ("standard", False),
        "ep_b": ("standard", False),
        "ep_c": ("flagship", False),
        "ep_emb": ("embedding", False),
    })
    standards = sorted(r._pick_endpoints_by_tier("standard"))
    assert standards == ["ep_a", "ep_b"]
    assert r._pick_endpoints_by_tier("flagship") == ["ep_c"]
    # Embedding tier always excluded (chat-only fallback chain).
    assert r._pick_endpoints_by_tier("embedding") == []
    assert r._pick_endpoints_by_tier("") == []


def test_build_attempts_includes_all_standard_endpoints():
    """With two standard-tier endpoints, both appear in attempts."""
    r = _make_router({
        "ep_a": ("standard", False),
        "ep_b": ("standard", False),
    })
    attempts = r._build_tier_attempts(preferred_tier="standard")
    keys = [ep for _, ep, _ in attempts]
    assert set(keys) == {"ep_a", "ep_b"}, f"expected both standard eps, got {keys}"


def test_build_attempts_safety_net_picks_remaining_chat_eps():
    """Endpoints not in any walked tier still get appended via the safety net."""
    r = _make_router({
        "ep_std": ("standard", False),
        "ep_distill": ("distill", False),
    })
    # Default chain is [flagship, standard, small]; distill is not in it,
    # but the safety net guarantees it is still tried.
    attempts = r._build_tier_attempts(preferred_tier="standard")
    keys = [ep for _, ep, _ in attempts]
    assert "ep_std" in keys and "ep_distill" in keys


def test_build_attempts_safety_net_excludes_embedding():
    """Embedding endpoints never enter the chat fallback chain."""
    r = _make_router({
        "ep_std": ("standard", False),
        "ep_emb": ("embedding", False),
    })
    attempts = r._build_tier_attempts(preferred_tier="standard")
    keys = [ep for _, ep, _ in attempts]
    assert keys == ["ep_std"], f"embedding leaked into chat chain: {keys}"


@pytest.mark.asyncio
async def test_generate_with_tier_fallback_two_standards_first_fails():
    """Two standard endpoints — when the first fails, fallback hits the second.

    Mirrors the prod scenario: 本地 Qwen (standard) returns stream INTERNAL_ERROR,
    chain must auto-switch to 大纲 (also standard) instead of giving up.
    """
    r = _make_router({
        "ep_qwen": ("standard", True),    # mimic 本地 Qwen 瞬态 stream error
        "ep_outline": ("standard", False), # mimic 大纲 endpoint healthy
    })
    result = await r.generate_with_tier_fallback(
        task_type="evaluation",
        messages=[{"role": "user", "content": "hi"}],
        preferred_tier="standard",
    )
    # Should land on the second standard endpoint, not raise.
    assert result.text in ("ok-ep_qwen", "ok-ep_outline")
    assert result.text == "ok-ep_outline"
    assert r.providers["ep_qwen"].calls == 1
    assert r.providers["ep_outline"].calls == 1


# ===========================================================================
# Caller-site tests: ChapterEvaluator + checkers must use tier-fallback path
# ===========================================================================

@pytest.mark.asyncio
async def test_chapter_evaluator_uses_generate_with_tier_fallback():
    """ChapterEvaluator.evaluate() must call generate_with_tier_fallback,
    not the legacy generate() (which would re-introduce the P0 bug)."""
    from app.services.chapter_evaluator import ChapterEvaluator

    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=GenerationResult(
            text='{'
                 '"plot_coherence":{"score":8.0,"issues":[]},'
                 '"character_consistency":{"score":7.5,"issues":[]},'
                 '"style_adherence":{"score":8.2,"issues":[]},'
                 '"narrative_pacing":{"score":7.8,"issues":[]},'
                 '"foreshadow_handling":{"score":7.6,"issues":[]}'
                 '}',
            usage=TokenUsage(input_tokens=100, output_tokens=200),
            model="stub", provider="stub",
        )
    )
    fake_router.generate = AsyncMock(side_effect=AssertionError(
        "legacy generate() must NOT be called — that's the P0 bug."))

    with patch(
        "app.services.chapter_evaluator.get_model_router_async",
        AsyncMock(return_value=fake_router),
    ):
        ev = ChapterEvaluator()
        result = await ev.evaluate(
            chapter_text="本章测试内容。" * 50,
            chapter_outline={"summary": "测试章节大纲"},
            previous_summary="", style_profile="", active_foreshadows=[],
        )

    fake_router.generate_with_tier_fallback.assert_awaited_once()
    call_kwargs = fake_router.generate_with_tier_fallback.await_args.kwargs
    assert call_kwargs.get("task_type") == "evaluation"
    # _log_meta tag is set so llm_call_logs distinguish evaluator calls.
    assert call_kwargs.get("_log_meta", {}).get("caller") == "chapter_evaluator.evaluate"
    assert result.overall == pytest.approx(7.82, abs=0.01)


@pytest.mark.asyncio
async def test_consistency_checker_llm_uses_tier_fallback():
    from app.services.checkers.consistency_checker import ConsistencyChecker
    from app.services.checkers.base import CheckResult

    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=GenerationResult(
            text='[]',  # no issues
            usage=TokenUsage(input_tokens=50, output_tokens=2),
            model="stub", provider="stub",
        )
    )
    fake_router.generate = AsyncMock(side_effect=AssertionError(
        "consistency_checker must use tier-fallback path"))

    # ContextPack with minimal world_rules + character_cards.
    ctx = MagicMock()
    ctx.world_rules = ["rule1", "rule2"]
    ctx.character_cards = []

    result = CheckResult(checker_name="consistency")
    with patch(
        "app.services.model_router.get_model_router_async",
        AsyncMock(return_value=fake_router),
    ):
        checker = ConsistencyChecker()
        await checker._llm_consistency_check("text", ctx, result)

    fake_router.generate_with_tier_fallback.assert_awaited_once()
    call_kwargs = fake_router.generate_with_tier_fallback.await_args.kwargs
    assert call_kwargs.get("task_type") == "extraction"
    assert call_kwargs.get("_log_meta", {}).get("caller") == \
        "consistency_checker._llm_consistency_check"


@pytest.mark.asyncio
async def test_ooc_checker_llm_uses_tier_fallback():
    from app.services.checkers.ooc_checker import OOCChecker
    from app.services.checkers.base import CheckResult

    fake_router = MagicMock()
    fake_router.generate_with_tier_fallback = AsyncMock(
        return_value=GenerationResult(
            text='[]',
            usage=TokenUsage(input_tokens=50, output_tokens=2),
            model="stub", provider="stub",
        )
    )
    fake_router.generate = AsyncMock(side_effect=AssertionError(
        "ooc_checker must use tier-fallback path"))

    # Build a minimal character card with .name and .to_prompt().
    card = MagicMock()
    card.name = "测试角色"
    card.to_prompt = MagicMock(return_value="测试角色的设定")

    result = CheckResult(checker_name="ooc")
    with patch(
        "app.services.model_router.get_model_router_async",
        AsyncMock(return_value=fake_router),
    ):
        checker = OOCChecker()
        await checker._llm_ooc_check(
            "text", [card], {"测试角色": ["对话1", "对话2"]}, result,
        )

    fake_router.generate_with_tier_fallback.assert_awaited_once()
    call_kwargs = fake_router.generate_with_tier_fallback.await_args.kwargs
    assert call_kwargs.get("task_type") == "extraction"
    assert call_kwargs.get("_log_meta", {}).get("caller") == \
        "ooc_checker._llm_ooc_check"
