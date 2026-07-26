"""Memory-subsystem residual fixes (global axis, W9, W5, W5b, token budget).

Covers:
1. /strand-status computes "current chapter" on Chapter.global_idx (book
   global), falling back to the old local-idx max for NULL global_idx rows.
2. HierarchicalMemory.assemble converts the volume-local chapter idx to the
   book-global axis before the Neo4j entity-state snapshot (L5).
3. W9: _maybe_rewrite_query consumes the actual rag_query_rewrite schema
   {queries: [...], keywords: [...]} (legacy shapes still accepted).
4. W5: _cap_character_cards bounds full-card rendering to chapter-relevant
   characters + protagonists; relevant overflow survives name-only in the
   Layer-0 roster; small casts are untouched.
5. W5b: _overlay_character_states overlays the freshest character_states row
   onto card fields, keeping profile_json as fallback.
6. ContextPack.to_system_prompt default budget reads
   settings.CONTEXT_PACK_TOKEN_BUDGET.

All tests run offline (fake async sessions, mocked services).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PID = "f14712d6-6dc6-4cfb-b05f-e107fa02b63d"
VOL1_ID = str(uuid.uuid4())
VOL2_ID = str(uuid.uuid4())


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        if self._scalar is not None:
            return self._scalar
        row = self.first()
        if row is None:
            return None
        return row[0] if isinstance(row, (tuple, list)) else row


class _FakeDB:
    """Async-session stub: returns queued results, records statements."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.statements = []

    async def execute(self, stmt, *a, **kw):
        self.statements.append(stmt)
        if not self._responses:
            return _FakeResult()
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# 1. quality.py /strand-status — book-global current chapter
# ---------------------------------------------------------------------------


def _mock_tracker_cls():
    strand_data = MagicMock()
    strand_data.last_quest_chapter = 0
    strand_data.last_fire_chapter = 0
    strand_data.last_constellation_chapter = 0
    strand_data.current_dominant = "quest"
    strand_data.get_warnings = MagicMock(return_value=[])

    svc = MagicMock()
    svc.analyze_strands = AsyncMock(return_value=strand_data)
    svc.get_strand_history = AsyncMock(return_value=[])
    svc.get_balance_recommendations = MagicMock(return_value=[])
    cls = MagicMock(return_value=svc)
    return cls, svc, strand_data


@pytest.mark.asyncio
async def test_strand_status_uses_global_idx():
    from app.api.quality import get_strand_status

    db = _FakeDB(
        [
            _FakeResult(rows=[VOL1_ID, VOL2_ID]),   # volume ids
            _FakeResult(scalar=751),                # max global_idx
        ]
    )
    cls, svc, strand_data = _mock_tracker_cls()
    with patch("app.services.strand_tracker.StrandTrackerService", cls):
        await get_strand_status(PID, db=db)

    svc.analyze_strands.assert_awaited_once_with(PID, 751)
    strand_data.get_warnings.assert_called_once_with(751)
    # The chapter query must be on the global axis and exclude NULL rows.
    assert "global_idx" in str(db.statements[1])


@pytest.mark.asyncio
async def test_strand_status_null_global_idx_falls_back_to_local():
    from app.api.quality import get_strand_status

    db = _FakeDB(
        [
            _FakeResult(rows=[VOL1_ID]),   # volume ids
            _FakeResult(rows=[]),          # no non-NULL global_idx rows
            _FakeResult(scalar=12),        # old local-idx max
        ]
    )
    cls, svc, _ = _mock_tracker_cls()
    with patch("app.services.strand_tracker.StrandTrackerService", cls):
        await get_strand_status(PID, db=db)

    svc.analyze_strands.assert_awaited_once_with(PID, 12)
    assert "chapter_idx" in str(db.statements[2])


# ---------------------------------------------------------------------------
# 2. memory.py — assemble resolves the global axis for L5
# ---------------------------------------------------------------------------


def _patched_memory(db):
    from app.services.memory import HierarchicalMemory

    mem = HierarchicalMemory(db=db, neo4j_driver=MagicMock())
    mem._gather_world_state = AsyncMock(return_value="")
    mem._gather_volume_summaries = AsyncMock(return_value="")
    mem._gather_chapter_summaries = AsyncMock(return_value="")
    mem._gather_recent_text = AsyncMock(return_value="")
    mem._gather_entity_states = AsyncMock(return_value="")
    return mem


@pytest.mark.asyncio
async def test_assemble_passes_global_idx_to_entity_states():
    db = _FakeDB([_FakeResult(scalar=751)])  # Chapter.global_idx lookup
    mem = _patched_memory(db)

    await mem.assemble(PID, VOL2_ID, 1)

    mem._gather_entity_states.assert_awaited_once_with(PID, 751)
    # Layers filtering by volume stay on the volume-local axis.
    mem._gather_recent_text.assert_awaited_once_with(VOL2_ID, 1)
    assert "global_idx" in str(db.statements[0])


@pytest.mark.asyncio
async def test_assemble_falls_back_to_local_idx_when_global_null():
    db = _FakeDB([_FakeResult(rows=[])])  # NULL / missing global_idx
    mem = _patched_memory(db)

    await mem.assemble(PID, VOL1_ID, 7)

    mem._gather_entity_states.assert_awaited_once_with(PID, 7)


# ---------------------------------------------------------------------------
# 3. W9 — rag_query_rewrite consumer accepts the actual prompt schema
# ---------------------------------------------------------------------------


def _builder_with_db(db):
    from app.services.context_pack import ContextPackBuilder

    return ContextPackBuilder(db=db)


@pytest.mark.asyncio
async def test_rewrite_consumes_queries_and_keywords(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_REWRITE_ENABLED", "1")
    builder = _builder_with_db(MagicMock())
    out = {"queries": ["主角 突破", "宗门大比"], "keywords": ["丹药", "主角 突破"]}
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        AsyncMock(return_value=out),
    ):
        result = await builder._maybe_rewrite_query("原始查询")
    # Joined, deduplicated, order preserved.
    assert result == "主角 突破 宗门大比 丹药"


@pytest.mark.asyncio
async def test_rewrite_legacy_query_key_still_accepted(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_REWRITE_ENABLED", "1")
    builder = _builder_with_db(MagicMock())
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        AsyncMock(return_value={"query": "改写后"}),
    ):
        assert await builder._maybe_rewrite_query("原始查询") == "改写后"


@pytest.mark.asyncio
async def test_rewrite_empty_output_returns_original(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_REWRITE_ENABLED", "1")
    builder = _builder_with_db(MagicMock())
    with patch(
        "app.services.prompt_registry.run_structured_prompt",
        AsyncMock(return_value={"queries": [], "keywords": []}),
    ):
        assert await builder._maybe_rewrite_query("原始查询") == "原始查询"


@pytest.mark.asyncio
async def test_rewrite_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("RAG_QUERY_REWRITE_ENABLED", raising=False)
    builder = _builder_with_db(MagicMock())
    prompt_mock = AsyncMock()
    with patch("app.services.prompt_registry.run_structured_prompt", prompt_mock):
        assert await builder._maybe_rewrite_query("原始查询") == "原始查询"
    prompt_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. W5 — character card capping
# ---------------------------------------------------------------------------


def _make_pack(names, outline=None, content=""):
    from app.services.context_pack import CharacterCard, ContextPack

    pack = ContextPack()
    pack.character_cards = [CharacterCard(name=n) for n in names]
    pack.current_outline = outline or {}
    pack.current_content = content
    return pack


# 20 substring-safe names: A君..T君
CAST20 = [f"{c}君" for c in "ABCDEFGHIJKLMNOPQRST"]


@pytest.mark.asyncio
async def test_cap_keeps_mentioned_then_protagonists(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CONTEXT_PACK_MAX_CHARACTER_CARDS", 4)
    pack = _make_pack(
        CAST20,
        outline={"summary": "C君与D君对决，E君旁观", "key_points": ["Q君现身"]},
        content="R君推门而入。",
    )
    # A/B are protagonists by appearance count but not mentioned this chapter.
    counts = _FakeResult(rows=[("A君", 100), ("B君", 90)])
    builder = _builder_with_db(_FakeDB([counts]))

    await builder._cap_character_cards(pack, PID)

    kept = [c.name for c in pack.character_cards]
    # 5 mentioned > cap 4: first 4 mentioned (original order) get full cards.
    assert kept == ["C君", "D君", "E君", "Q君"]
    # The remaining mentioned name survives name-only; unmentioned cast
    # (including the protagonists that lost the slot race) is dropped.
    assert pack.roster_extra_names == ["R君"]

    prompt = pack.to_system_prompt()
    assert "R君" in prompt          # name-only in Layer-0 roster
    assert "A君" not in prompt      # not relevant to this chapter
    assert "T君" not in prompt


@pytest.mark.asyncio
async def test_cap_protagonists_fill_remaining_slots(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CONTEXT_PACK_MAX_CHARACTER_CARDS", 4)
    pack = _make_pack(CAST20, outline={"summary": "C君闭关。"})
    counts = _FakeResult(rows=[("T君", 100), ("S君", 90)])
    builder = _builder_with_db(_FakeDB([counts]))

    await builder._cap_character_cards(pack, PID)

    kept = [c.name for c in pack.character_cards]
    # Mentioned first, protagonists by count next, then original order.
    assert kept == ["A君", "C君", "S君", "T君"]  # original card order preserved
    assert pack.roster_extra_names == []


@pytest.mark.asyncio
async def test_cap_default_is_12_from_settings():
    pack = _make_pack(CAST20, outline={"summary": "C君与D君对决"})
    builder = _builder_with_db(_FakeDB([_FakeResult(rows=[])]))

    await builder._cap_character_cards(pack, PID)

    assert len(pack.character_cards) == 12


@pytest.mark.asyncio
async def test_small_cast_is_untouched():
    names = ["甲", "乙", "丙", "丁", "戊"]
    pack = _make_pack(names, outline={"summary": "甲与乙对决"})
    db = _FakeDB([])
    builder = _builder_with_db(db)

    before = pack.to_system_prompt()
    await builder._cap_character_cards(pack, PID)

    assert [c.name for c in pack.character_cards] == names
    assert pack.roster_extra_names == []
    assert pack.to_system_prompt() == before
    assert db.statements == []  # early-exit: no queries for small casts


# ---------------------------------------------------------------------------
# 5. W5b — character_states overlay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_overlay_fresher_state_wins():
    from app.services.context_pack import CharacterCard, ContextPack

    pack = ContextPack()
    pack.character_cards = [
        CharacterCard(name="甲", power_level="炼气", mental_state="平静"),
        CharacterCard(name="乙", power_level="金丹", location="王都"),
        CharacterCard(name="丙", power_level="元婴"),
    ]
    # Rows arrive chapter_start DESC (as the query orders them): the first
    # row per character is the freshest and must win over both older rows
    # and the static profile values. 乙's row is a raw JSON string, as
    # materialized by entity_tasks.
    rows = _FakeResult(
        rows=[
            ("甲", {"能力等级": "筑基", "情绪": "愤怒", "位置": "青云山"}),
            ("甲", {"能力等级": "炼气", "情绪": "平静"}),
            ("乙", '{"状态": "重伤", "位置": "北境"}'),
        ]
    )
    builder = _builder_with_db(_FakeDB([rows]))

    await builder._overlay_character_states(pack, PID, 751)

    jia, yi, bing = pack.character_cards
    assert jia.power_level == "筑基"        # fresher state wins
    assert jia.mental_state == "愤怒"
    assert jia.location == "青云山"          # empty location filled
    assert yi.mental_state == "重伤"         # JSON-string status parsed
    assert yi.power_level == "金丹"          # no 能力等级 in state → fallback
    assert yi.location == "王都"             # projection location authoritative
    assert bing.power_level == "元婴"        # no state row → profile intact


@pytest.mark.asyncio
async def test_state_overlay_queries_global_axis():
    from app.services.context_pack import CharacterCard, ContextPack

    pack = ContextPack()
    pack.character_cards = [CharacterCard(name="甲")]
    db = _FakeDB([_FakeResult(rows=[])])
    builder = _builder_with_db(db)

    await builder._overlay_character_states(pack, PID, 751)

    sql = str(db.statements[0])
    assert "character_states" in sql
    assert "chapter_start" in sql


# ---------------------------------------------------------------------------
# 6. Token budget from settings
# ---------------------------------------------------------------------------


def test_token_budget_default_reads_settings(monkeypatch):
    from app.config import settings
    from app.services.context_pack import ContextPack

    pack = ContextPack()
    pack.current_content = "测" * 9000

    baseline = pack.to_system_prompt(token_budget=9500)
    assert pack.to_system_prompt() == baseline  # default == setting default

    monkeypatch.setattr(settings, "CONTEXT_PACK_TOKEN_BUDGET", 200)
    shrunk = pack.to_system_prompt()
    assert len(shrunk) < len(baseline)
    # Explicit argument still overrides the setting.
    assert pack.to_system_prompt(token_budget=9500) == baseline
