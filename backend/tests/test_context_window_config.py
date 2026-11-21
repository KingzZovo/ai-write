"""回归:上下文窗口与 token 换算的校准(2026-11-16)。

两条被实测纠正的事实,都必须钉住,否则会被"看起来更合理"的改法再次弄坏:

1. **汉字/token 真实约 1.43~1.48,不是 4.00。**
   实测(网关 ``http://127.0.0.1:8317``,`deepseek-chat`,真实章节正文):
   1654 字→1116 token、4321 字→2953、8093 字→5583、14248 字→9945、
   20000 字→13936、40000 字→27892(线性)。
   所以 ``CHARS_PER_TOKEN = 1.5`` 本来就是对的。此前报告里
   "4.00 汉字/token"来自另一组测法,与本口径不符,已作废;
   谁要是把常量改成 4.0,预算会实际超发约 2.7 倍,必须让这条测试失败。

2. **窗口原先写死在 SQL/切片里,先于预算生效。**
   ``previous_chapter_text`` 是 required 块(放大默认值会让
   ``ContextBudgetExceeded`` 变成真实故障),所以它维持 3500;
   其余(前文摘要条数、后文章纲条数、全书大纲头/尾)改为可配置。
   预算 9500 → 20000 后,``book_outline`` 才不再被整块丢弃 ——
   ``allocate_context`` 对超额度块是**整块省略**,不做截断。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.character_context import CharacterCard
from app.services.context_pack import CHARS_PER_TOKEN, ContextPack


def test_chars_per_token_matches_measured_ratio():
    """真实正文约 1.43~1.48 汉字/token;1.5 是略偏乐观的保守估计。"""
    assert 1.3 <= CHARS_PER_TOKEN <= 1.6, (
        "CHARS_PER_TOKEN 必须落在实测区间(1.43~1.48 汉字/token)。"
        "改成 4.0 会让 token 预算实际超发约 2.7 倍。"
    )


def test_window_settings_have_documented_defaults():
    # 前章正文是 required 块:默认必须与单章目标同量级,放大它等于制造故障。
    assert settings.CONTEXT_PREV_CHAPTER_TAIL_CHARS == 3500
    assert settings.CONTEXT_RECENT_SUMMARY_CHAPTERS == 8
    assert settings.CONTEXT_FUTURE_OUTLINE_CHAPTERS == 20
    assert settings.CONTEXT_BOOK_OUTLINE_HEAD_CHARS == 4000
    assert settings.CONTEXT_BOOK_OUTLINE_TAIL_CHARS == 2000
    assert settings.CONTEXT_FUTURE_OUTLINE_MAX_CHARS == 3000
    # 抬高预算正是为了让"全书大纲"这类大块进得去。
    assert settings.CONTEXT_PACK_TOKEN_BUDGET >= 20000


def _pack_with_long_book_outline() -> ContextPack:
    """一份**合成**的长书装配需求:各块大小按 ch18 量级放大,不用真实库。"""
    return ContextPack(
        book_outline_excerpt="全书大纲内容" * 1000,                      # 6000 字
        volume_outline={"title": "渡口旧账", "core_conflict": "铺租到期" * 30,
                        "turning_points": ["查明旧账", "结清工钱"]},
        recent_summaries=[f"[前文-{i}] 第{i}章摘要" * 20 for i in range(8)],
        previous_chapter_text="【紧邻前章当前正文】" + "上一章正文内容" * 580,
        current_outline={"summary": "周芸去铺子结账" * 20, "key_events": ["结账"]},
        future_outlines=[f"[后续-{i}] 第{i}章走向" * 8 for i in range(20)],
        authoritative_fact_contract="事实" * 300,
        world_rules=["规则" * 60 for _ in range(6)],
        writing_rules=["写作规则" * 40 for _ in range(4)],
        character_cards=[CharacterCard(name="周芸", mental_state="焦虑" * 60)],
        cognition_boundaries="认知边界" * 80,
        contradiction_cache=["矛盾" * 40],
        compass_anchor="锚点" * 100,
        related_chapter_recall="历史回读" * 200,
    )


def _status(pack: ContextPack) -> dict[str, str]:
    return {row["source"]: row["status"] for row in pack.budget_trace}


def test_book_outline_is_included_at_default_budget():
    """默认预算下低优先级大块必须进提示;旧预算(9500)下它被整块省略。

    这是**合成**需求(刻意把全书大纲放大到 6000 字)。真实 24 章项目里被整块丢掉的
    是 `future_outlines`(ch7 丢 3,013 字),不是 `book_outline`(只有 101 字)。
    见 docs/evaluation/context-window-measurement-20261115.md §3。
    """
    pack = _pack_with_long_book_outline()
    pack.to_system_prompt()
    assert _status(pack)["book_outline"] == "included"

    old = _pack_with_long_book_outline()
    old.to_system_prompt(token_budget=9500)
    assert _status(old)["book_outline"] == "omitted_budget", (
        "这条断言语义是\"旧预算装不下\":allocate_context 对超额度块整块省略,"
        "所以加大块而不加预算等于白加。"
    )


@pytest.mark.parametrize("source", ["previous_chapter_text", "current_outline"])
def test_required_blocks_are_never_dropped(source):
    pack = _pack_with_long_book_outline()
    pack.to_system_prompt()
    assert _status(pack)[source] == "included"


def test_future_outlines_block_is_capped_and_keeps_nearest_chapters():
    """后文章纲:整块被省略 = 写手完全看不到后续走向;改为截尾保近。"""
    pack = ContextPack(
        current_outline={"summary": "本章任务"},
        future_outlines=[f"第{i}章的后续走向" * 20 for i in range(1, 31)],
    )
    prompt = pack.to_system_prompt()
    assert "[后续-1]" in prompt, "最近的后续章必须留下"
    assert "[后续-30]" not in prompt, "最远的后续章应被截掉"
    assert "更远的后章走向因篇幅省略" in prompt
    block = next(r for r in pack.budget_trace if r["source"] == "future_outlines")
    assert block["included_chars"] <= settings.CONTEXT_FUTURE_OUTLINE_MAX_CHARS + 40


def test_numeric_pins_still_sees_all_future_outlines_despite_the_cap():
    """截的是渲染块,不是 pack.future_outlines —— 金额钉子要从完整列表取。"""
    pack = ContextPack(
        current_outline={"summary": "本章任务"},
        future_outlines=["第%d章的后续走向" % i * 20 for i in range(1, 31)]
        + ["最后一段写明工钱总额八百二十块"],
    )
    prompt = pack.to_system_prompt()
    assert "八百二十" in prompt
