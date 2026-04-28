"""v1.7.4 P1-alpha tests: ContextPackBuilder._load_style_samples + helpers.

Covers the three resolution paths after the rewrite:
  A) StyleProfile via settings.style_reference.profile_id
  A') StyleProfile via legacy settings.default_style_profile_id
  B) StyleProfileCard fallback via settings.style_reference.reference_book_id
  Z) Empty path (no settings, no binding) -> style_samples stays empty.

Also covers the two pure helpers in isolation:
  - _render_style_profile  (StyleProfile ORM row -> list[str])
  - _aggregate_style_cards (cards table query + 9-dim aggregation)
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.context_pack import ContextPack, ContextPackBuilder


# ---------------------------------------------------------------------------
# _render_style_profile
# ---------------------------------------------------------------------------


def test_render_style_profile_full():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    profile = SimpleNamespace(
        name="龙族风格",
        source_book="龙族全套",
        rules_json=[
            {"rule": "长短句强烈交错"},
            {"text": "对话口语化与市井俚语"},
            "避免庄严抒情",
        ],
        anti_ai_rules=[
            {"pattern": "打字者心头一热"},
            "泪水不听使唤的备派上场",
        ],
        tone_keywords=["市井", "口语", "轻喜剧"],
        sample_passages=[
            {"text": "路明非看着马尔安。"},
            "他原本以为这只是另一场口试。",
        ],
    )
    parts = builder._render_style_profile(profile)
    assert parts, "should produce at least one block"
    joined = "\n".join(parts)
    assert "龙族全套" in joined
    assert "长短句强烈交错" in joined
    assert "对话口语化与市井俚语" in joined  # dict text key
    assert "避免庄严抒情" in joined  # bare string entry
    assert "禁用:" in joined and "打字者心头一热" in joined
    assert "语气词汇" in joined and "市井" in joined
    assert "路明非看着马尔安" in joined


def test_render_style_profile_empty_returns_empty_list():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    profile = SimpleNamespace(
        name="empty", source_book=None,
        rules_json=None, anti_ai_rules=[], tone_keywords=None, sample_passages=None,
    )
    assert builder._render_style_profile(profile) == []


# ---------------------------------------------------------------------------
# _aggregate_style_cards
# ---------------------------------------------------------------------------


def _make_card(profile_dict):
    return SimpleNamespace(profile_json=profile_dict)


@pytest.mark.asyncio
async def test_aggregate_style_cards_basic():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    cards = [
        _make_card({
            "pov": "third_limited",
            "tense": "past",
            "sentence_rhythm": "长短句交错",
            "dialogue_style": "口语化、带市井俚语",
            "sensory_mix": {"visual": 0.6, "auditory": 0.3, "touch": 0.1},
            "pacing": "先慢后快",
            "emotional_register": "谐谑中带酸涩",
            "vocab_tone": ["市井", "口语"],
            "forbidden_tells": ["避免庄严抒情", "避免古风雅言"],
            "signature_moves": ["脑内弹幕", "自嘲定义战斗力"],
        }),
        _make_card({
            "pov": "third_limited",
            "tense": "past",
            "sentence_rhythm": "长短句交错, 多插入内心独白字超过二十字",
            "dialogue_style": "反问与夸张式赞美",
            "sensory_mix": {"visual": 0.7, "auditory": 0.2, "touch": 0.1},
            "vocab_tone": ["轻喜剧", "市井"],
            "forbidden_tells": ["避免庄严抒情"],
        }),
    ]
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_scalars = MagicMock()
    fake_scalars.all = MagicMock(return_value=cards)
    fake_result.scalars = MagicMock(return_value=fake_scalars)
    fake_db.execute = AsyncMock(return_value=fake_result)

    parts = await builder._aggregate_style_cards(fake_db, "book-uuid", top_k=10)
    assert parts, "should aggregate to at least 1 block"
    joined = "\n".join(parts)
    assert "third_limited past" in joined  # voted pov + tense
    # longest rhythm wins
    assert "多插入内心独白字超过二十字" in joined
    assert "感官分布" in joined  # sensory averaged
    assert "visual" in joined
    assert "市井" in joined  # vocab union
    assert "轻喜剧" in joined
    assert "禁忌:" in joined and "避免庄严抒情" in joined
    assert "招牌:" in joined and "脑内弹幕" in joined


@pytest.mark.asyncio
async def test_aggregate_style_cards_empty_db():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    fake_db = MagicMock()
    fake_result = MagicMock()
    fake_scalars = MagicMock()
    fake_scalars.all = MagicMock(return_value=[])
    fake_result.scalars = MagicMock(return_value=fake_scalars)
    fake_db.execute = AsyncMock(return_value=fake_result)
    parts = await builder._aggregate_style_cards(fake_db, "missing-book", top_k=5)
    assert parts == []


@pytest.mark.asyncio
async def test_aggregate_style_cards_swallows_exception():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    parts = await builder._aggregate_style_cards(fake_db, "any", top_k=5)
    assert parts == []  # silent degrade


# ---------------------------------------------------------------------------
# _load_style_samples (top-level, end-to-end with mocks)
# ---------------------------------------------------------------------------


class _FakeProject:
    def __init__(self, settings_json):
        self.settings_json = settings_json


@pytest.mark.asyncio
async def test_load_style_samples_path_a_legacy_default_key():
    """Legacy settings_json={'default_style_profile_id': X} should resolve via Path A."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=["BLOCK_A"])
    builder._aggregate_style_cards = AsyncMock(return_value=["SHOULD_NOT_USE"])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(side_effect=[
        _FakeProject({"default_style_profile_id": "profile-uuid"}),
        SimpleNamespace(name="X"),  # StyleProfile fetched
    ])
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj-uuid")
    assert pack.style_samples == ["BLOCK_A"]
    builder._aggregate_style_cards.assert_not_called()  # path A short-circuited


@pytest.mark.asyncio
async def test_load_style_samples_path_a_new_style_reference_key():
    """New settings.style_reference.profile_id should also resolve via Path A."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=["BLOCK_NEW"])
    builder._aggregate_style_cards = AsyncMock(return_value=[])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(side_effect=[
        _FakeProject({"style_reference": {"profile_id": "p"}}),
        SimpleNamespace(name="Y"),
    ])
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == ["BLOCK_NEW"]


@pytest.mark.asyncio
async def test_load_style_samples_path_b_cards_fallback():
    """When no profile binding but reference_book_id is set, Path B (cards) runs."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=[])
    builder._aggregate_style_cards = AsyncMock(return_value=["BLOCK_B1", "BLOCK_B2"])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(return_value=_FakeProject({
        "style_reference": {"reference_book_id": "book-uuid"}
    }))
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == ["BLOCK_B1", "BLOCK_B2"]
    builder._aggregate_style_cards.assert_called_once()
    args, kwargs = builder._aggregate_style_cards.call_args
    # signature: (db, book_id, top_k=12)
    assert args[1] == "book-uuid"


@pytest.mark.asyncio
async def test_load_style_samples_path_b_top_level_reference_book_id():
    """Top-level settings.reference_book_id should also work for Path B."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=[])
    builder._aggregate_style_cards = AsyncMock(return_value=["BLOCK_TOPLEVEL"])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(return_value=_FakeProject({"reference_book_id": "top-book"}))
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == ["BLOCK_TOPLEVEL"]


@pytest.mark.asyncio
async def test_load_style_samples_no_binding_yields_empty():
    """Project with empty settings -> style_samples stays untouched (no error)."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=[])
    builder._aggregate_style_cards = AsyncMock(return_value=[])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(return_value=_FakeProject({}))
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == []
    builder._render_style_profile.assert_not_called()
    builder._aggregate_style_cards.assert_not_called()


@pytest.mark.asyncio
async def test_load_style_samples_path_a_falls_through_to_b_when_render_empty():
    """If profile_id resolves but rendering is empty, Path B should still try."""
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._render_style_profile = MagicMock(return_value=[])  # empty render
    builder._aggregate_style_cards = AsyncMock(return_value=["FALLBACK"])

    fake_db = MagicMock()
    fake_db.get = AsyncMock(side_effect=[
        _FakeProject({
            "default_style_profile_id": "profile-x",
            "reference_book_id": "book-y",
        }),
        SimpleNamespace(name="X"),  # StyleProfile (but render returns [])
    ])
    builder._get_db = AsyncMock(return_value=fake_db)

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == ["FALLBACK"]
    builder._aggregate_style_cards.assert_called_once()


@pytest.mark.asyncio
async def test_load_style_samples_swallows_db_exception():
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._get_db = AsyncMock(side_effect=RuntimeError("db down"))

    pack = ContextPack()
    await builder._load_style_samples(pack, project_id="proj")
    assert pack.style_samples == []  # silent degrade
