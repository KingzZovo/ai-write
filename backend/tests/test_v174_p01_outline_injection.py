"""v1.7.4 P0-1 regression: ContextPack injects book/volume outline.

Before v1.7.4 the chapter-generation system prompt only contained the
current-chapter beat outline. It did not pull anything from the `outlines`
table, so the model never saw the global picture. This test pins down the
new behavior.
"""
from __future__ import annotations

from app.services.context_pack import ContextPack


class TestContextPackOutlineFields:
    def test_new_fields_default_values(self):
        pack = ContextPack()
        assert pack.book_outline_excerpt == ""
        assert pack.volume_outline == {}

    def test_book_outline_renders_at_top_of_l1(self):
        pack = ContextPack(
            book_outline_excerpt="主题：被记住，才算真正活过",
        )
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【全书大纲(节选)】" in prompt
        assert "被记住" in prompt
        assert prompt.index("【全书大纲(节选)】") < 200

    def test_volume_outline_renders_all_subsections(self):
        pack = ContextPack(
            volume_outline={
                "title": "雾城失名",
                "volume_idx": 1,
                "core_conflict": "纪砚必须证明苏未存在",
                "emotional_arc": "从惊慌走向决绝",
                "new_characters": [
                    {"name": "裴归尘", "identity": "学宫馆长", "role": "导师反转"},
                    {"name": "罗弥", "identity": "契约商人", "role": "黑市中介"},
                ],
                "turning_points": ["发现火痕", "卷末被劫"],
                "foreshadows": {
                    "planted": [
                        {"description": "锈钟塔涂鸦", "resolve_conditions": ["对应灰井", "路径密码"]},
                    ],
                    "resolved": ["苏未是姄想被排除"],
                },
                "transition_to_next": "收尾为下卷埋伏笔",
            }
        )
        prompt = pack.to_system_prompt(token_budget=8000)
        assert "【本卷大纲】" in prompt
        assert "《第1卷 雾城失名》" in prompt
        assert "核心冲突：" in prompt
        assert "情感弧线：" in prompt
        assert "新登场角色：" in prompt
        assert "裴归尘" in prompt and "罗弥" in prompt
        assert "转折点：" in prompt
        assert "已埋伏笔：" in prompt
        assert "锈钟塔涂鸦" in prompt
        assert "卷末过渡：" in prompt

    def test_render_volume_outline_block_empty_returns_empty(self):
        pack = ContextPack()
        assert pack._render_volume_outline_block() == ""
        pack2 = ContextPack(volume_outline={})
        assert pack2._render_volume_outline_block() == ""

    def test_render_handles_missing_optional_fields(self):
        pack = ContextPack(volume_outline={"title": "only", "volume_idx": 2})
        block = pack._render_volume_outline_block()
        assert "《第2卷 only》" in block
        assert "核心冲突" not in block

    def test_render_handles_malformed_foreshadows(self):
        pack = ContextPack(volume_outline={
            "title": "x", "volume_idx": 1,
            "foreshadows": "not-a-dict",
            "new_characters": "not-a-list",
            "turning_points": None,
        })
        block = pack._render_volume_outline_block()
        assert "《第1卷 x》" in block
        assert "已埋伏笔" not in block
        assert "新登场角色" not in block
        assert "转折点" not in block

    def test_book_and_volume_outlines_both_render_in_order(self):
        pack = ContextPack(
            book_outline_excerpt="全书设定文本",
            volume_outline={"title": "v1", "volume_idx": 1, "core_conflict": "c"},
        )
        prompt = pack.to_system_prompt(token_budget=8000)
        i_book = prompt.index("【全书大纲(节选)】")
        i_vol = prompt.index("【本卷大纲】")
        assert i_book < i_vol, "book outline should appear before volume outline"
