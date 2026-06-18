"""Regression: scene_planner contract mismatch (2026-06-18).

``run_structured_prompt`` ALWAYS returns a dict — ``_normalize_structured``
wraps a bare JSON array under ``{"items": [...]}`` and passes ``{"scenes":
[...]}`` through unchanged. It never returns a bare ``list``.

``SceneOrchestrator.plan_scenes`` used ``isinstance(parsed_any, list)`` to
detect success, which is never true against the real contract, so every
well-formed planner output fell through to the deterministic fallback (the
"6/6 fallback" observed in production). The pre-existing happy-path tests
hid this by mocking ``run_structured_prompt`` to return a bare list — a shape
the real function never produces.

These tests pin the REAL contract: a dict with the scene array under
``items`` / ``scenes`` must yield the planner's own briefs, not the fallback
(whose titles are ``场景 N``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.scene_orchestrator import SceneOrchestrator

from tests.test_c1_scene_orchestrator import _FakePack, _scene_contract


def _four_briefs():
    return [
        _scene_contract(title="起", brief="起头", target_words=900, hook="h1"),
        _scene_contract(title="承", brief="转折", target_words=1100, hook="h2"),
        _scene_contract(title="转", brief="高潮", target_words=1100, hook="h3"),
        _scene_contract(title="合", brief="收尾", target_words=900, hook=""),
    ]


@pytest.mark.asyncio
async def test_plan_scenes_uses_items_wrapped_array():
    """Bare array normalized to {"items": [...]} must be used, not fallback."""
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"items": _four_briefs()}),
    ):
        out = await SceneOrchestrator().plan_scenes(
            pack=_FakePack(), db=None, project_id="p1", chapter_id="c1",
            target_words=4000, n_scenes_hint=4,
        )
    assert [b.title for b in out] == ["起", "承", "转", "合"]
    assert out[0].title != "场景 1"  # i.e. NOT the deterministic fallback


@pytest.mark.asyncio
async def test_plan_scenes_uses_scenes_wrapped_dict():
    """{"scenes": [...]} dict shape must be used, not fallback."""
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"scenes": _four_briefs()}),
    ):
        out = await SceneOrchestrator().plan_scenes(
            pack=_FakePack(), db=None, project_id="p1", chapter_id="c1",
            target_words=4000, n_scenes_hint=4,
        )
    assert [b.title for b in out] == ["起", "承", "转", "合"]


@pytest.mark.asyncio
async def test_plan_scenes_falls_back_on_parse_error_dict():
    """A genuine parse failure ({"parse_error": True}) still falls back."""
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        new=AsyncMock(return_value={"raw_text": "not json", "parse_error": True}),
    ):
        out = await SceneOrchestrator().plan_scenes(
            pack=_FakePack(), db=None, project_id="p1", chapter_id="c1",
            target_words=4000, n_scenes_hint=4,
        )
    # fallback briefs use the deterministic "场景 N" titles
    assert out[0].title == "场景 1"
