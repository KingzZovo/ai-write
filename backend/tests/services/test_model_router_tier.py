"""Unit tests for the v1.4 three-level tier fallback helpers.

Covers ``compute_effective_tier(prompt_tier, endpoint_tier)`` and
``is_valid_tier(t)`` in ``app.services.model_router``:

    prompt.model_tier  ≫  endpoint.tier  ≫  "standard"

Invalid / empty inputs at each level must fall through silently, so the
result is always one of the five canonical tiers.
"""

from __future__ import annotations

import pytest

from app.services.model_router import (
    VALID_TIERS,
    compute_effective_tier,
    is_valid_tier,
)


class TestIsValidTier:
    def test_canonical_tier_set_matches_alembic(self) -> None:
        """VALID_TIERS must stay in sync with ck_*_tier CHECK constraints."""
        assert VALID_TIERS == frozenset(
            {"flagship", "standard", "small", "distill", "embedding"}
        )

    @pytest.mark.parametrize(
        "tier", ["flagship", "standard", "small", "distill", "embedding"]
    )
    def test_each_canonical_tier_is_valid(self, tier: str) -> None:
        assert is_valid_tier(tier) is True

    @pytest.mark.parametrize("bad", [None, "", "bogus", "Flagship", "STANDARD"])
    def test_invalid_and_empty_inputs_are_rejected(
        self, bad: str | None
    ) -> None:
        # case-sensitive on purpose—DB CHECK constraint is also lowercase
        assert is_valid_tier(bad) is False


class TestComputeEffectiveTier:
    def test_prompt_tier_wins_over_endpoint_tier(self) -> None:
        assert (
            compute_effective_tier("flagship", "small") == "flagship"
        )
        assert (
            compute_effective_tier("distill", "standard") == "distill"
        )

    def test_endpoint_tier_used_when_prompt_missing(self) -> None:
        assert compute_effective_tier(None, "small") == "small"
        assert compute_effective_tier("", "flagship") == "flagship"

    def test_defaults_to_standard_when_both_missing(self) -> None:
        assert compute_effective_tier(None, None) == "standard"
        assert compute_effective_tier("", "") == "standard"
        assert compute_effective_tier(None, "") == "standard"

    def test_invalid_prompt_tier_falls_through_to_endpoint(self) -> None:
        """A malformed prompt.model_tier must not poison routing."""
        assert compute_effective_tier("bogus", "small") == "small"
        assert compute_effective_tier("Flagship", "standard") == "standard"

    def test_invalid_at_both_levels_falls_back_to_standard(self) -> None:
        assert compute_effective_tier("bogus", "also-bogus") == "standard"
        assert compute_effective_tier("x", "y") == "standard"

    def test_embedding_tier_is_first_class(self) -> None:
        # embedding endpoints must not be silently downgraded
        assert (
            compute_effective_tier("embedding", "standard") == "embedding"
        )
        assert (
            compute_effective_tier(None, "embedding") == "embedding"
        )

    def test_result_is_always_a_valid_tier_string(self) -> None:
        # property-style sanity: output is always in VALID_TIERS
        inputs: list[tuple[str | None, str | None]] = [
            ("flagship", None),
            (None, "small"),
            (None, None),
            ("bogus", "bogus"),
            ("", ""),
            ("distill", "embedding"),
        ]
        for p, e in inputs:
            assert compute_effective_tier(p, e) in VALID_TIERS
