"""Tier-3 returning-character drill-down (细读层) — trigger, render, budget.

- deterministic trigger boundary: absent exactly GAP chapters → no; GAP+1 → yes
- 【旧人重现·原文片段】 render (header, 初登场 tag, 1200-char cap, silent absence)
- ContextPackBuilder wiring over mocked DB rows
- L3 layer order + keep-tail budget degradation (原文回读 head degrades first,
  旧人重现 tail survives)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.context_pack import (
    CharacterCard,
    ContextPack,
    ContextPackBuilder,
    is_returning_character,
    render_drilldown_block,
)


class TestTriggerBoundary:
    def test_gap_exactly_20_does_not_trigger(self):
        assert is_returning_character(80, 100, 20) is False

    def test_gap_21_triggers(self):
        assert is_returning_character(80, 101, 20) is True

    def test_recent_character_does_not_trigger(self):
        assert is_returning_character(99, 100, 20) is False


class TestRenderDrilldownBlock:
    def test_renders_header_cards_and_tags(self):
        entries = [
            {
                "name": "老K",
                "last_seen": 80,
                "cards": [
                    (3, "first_appearance", "老K推门而入，帽檐压得很低。"),
                    (80, "key_moment", "老K冷笑一声，把牌拍在桌上。"),
                ],
            }
        ]
        block = render_drilldown_block(entries, 101)
        assert block.startswith("【旧人重现·原文片段】")
        assert "▍老K（上次出场 [CH-80]，已隔21章）" in block
        assert "[CH-3·初登场] 老K推门而入" in block
        assert "[CH-80·片段] 老K冷笑一声" in block

    def test_empty_entries_silent_absence(self):
        assert render_drilldown_block([], 101) == ""
        assert render_drilldown_block(
            [{"name": "老K", "last_seen": 80, "cards": []}], 101
        ) == ""

    def test_total_cap_drops_tail_sections(self):
        entries = [
            {
                "name": f"角色{i}",
                "last_seen": 10,
                "cards": [(3, "key_moment", "废话" * 200)],  # 400 chars each
            }
            for i in range(5)
        ]
        block = render_drilldown_block(entries, 101, max_chars=1200)
        assert len(block) <= 1200 + 100  # header + fitted sections only
        assert "角色0" in block
        assert "角色4" not in block

    def test_deterministic(self):
        entries = [
            {"name": "老K", "last_seen": 80, "cards": [(3, "key_moment", "x")]}
        ]
        assert render_drilldown_block(entries, 101) == render_drilldown_block(
            entries, 101
        )


class TestBuilderDrilldown:
    @pytest.mark.asyncio
    async def test_returning_character_gets_block(self):
        db = AsyncMock()
        roster_result = MagicMock()
        roster_result.all.return_value = [("老K", 80)]
        cards_result = MagicMock()
        cards_result.all.return_value = [
            ("老K", 3, "first_appearance", "老K推门而入。"),
            ("老K", 40, "key_moment", "老K摔了杯子。"),
            ("老K", 80, "key_moment", "老K冷笑一声。"),
            ("老K", 20, "key_moment", "老K数着钱。"),
        ]
        db.execute = AsyncMock(side_effect=[roster_result, cards_result])
        builder = ContextPackBuilder(db=db)
        pack = ContextPack()
        pack.character_cards = [CharacterCard(name="老K")]

        await builder._build_returning_drilldown(pack, "pid", 101)  # gap 21

        block = pack.returning_character_drilldown
        assert "旧人重现" in block
        assert "[CH-3·初登场]" in block
        # Only the 2 most recent key_moments (40, 80), chronological order.
        assert "[CH-40·片段]" in block and "[CH-80·片段]" in block
        assert "[CH-20" not in block
        assert block.index("[CH-40·片段]") < block.index("[CH-80·片段]")

    @pytest.mark.asyncio
    async def test_gap_exactly_at_threshold_stays_silent(self):
        db = AsyncMock()
        roster_result = MagicMock()
        roster_result.all.return_value = [("老K", 80)]
        db.execute = AsyncMock(side_effect=[roster_result])
        builder = ContextPackBuilder(db=db)
        pack = ContextPack()
        pack.character_cards = [CharacterCard(name="老K")]

        await builder._build_returning_drilldown(pack, "pid", 100)  # gap 20

        assert pack.returning_character_drilldown == ""
        # Card fetch never happens when nothing triggers.
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_no_relevant_characters_no_queries(self):
        db = AsyncMock()
        builder = ContextPackBuilder(db=db)
        pack = ContextPack()

        await builder._build_returning_drilldown(pack, "pid", 101)

        assert pack.returning_character_drilldown == ""
        db.execute.assert_not_awaited()


class TestL3Rendering:
    def _pack(self) -> ContextPack:
        pack = ContextPack()
        pack.chunk_recall = ["[CH-4] " + "原文片段甲。" * 30]  # ~180 chars
        pack.rag_snippets = ["召回摘要一。", "召回摘要二。"]
        pack.returning_character_drilldown = (
            "【旧人重现·原文片段】\n▍老K（上次出场 [CH-80]，已隔21章）\n"
            "[CH-3·初登场] 老K推门而入。"
        )
        return pack

    def test_layer_order_head_to_tail(self):
        prompt = self._pack().to_system_prompt(token_budget=20000)
        assert "【原文回读】" in prompt
        assert "【旧人重现·原文片段】" in prompt
        assert (
            prompt.index("【原文回读】")
            < prompt.index("【相关片段】")
            < prompt.index("【旧人重现·原文片段】")
        )

    def test_budget_degradation_drops_chunk_recall_first(self):
        # L3 budget = 20% * 400 tokens * 1.5 chars = 120 chars: only the
        # tail (旧人重现) fits; the head (原文回读) is truncated away.
        prompt = self._pack().to_system_prompt(token_budget=400)
        assert "旧人重现" in prompt
        assert "【原文回读】" not in prompt

    def test_gates_render_nothing_when_absent(self):
        pack = ContextPack()
        pack.rag_snippets = ["召回摘要一。"]
        prompt = pack.to_system_prompt(token_budget=20000)
        assert "【原文回读】" not in prompt
        assert "旧人重现" not in prompt
