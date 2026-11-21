"""回归:按名目的金额台账(纯函数部分,不联网、不落库)。

实测背景(2026-11-17,真实章节链):旧口径"这个总额不在计划里就报"3 条命中里只有 1 条是真的,
两条误报都是**这个数正文里早就写过,只是大纲/摘要没记**(ch08 的 820 写在本章大纲里、
ch18 的 3000 早在 ch10 正文里)。根因是没有地方记"实际写出过的金额",于是
"新引入的金额"和"与前文冲突的金额"分不开 —— 台账补上这一格。

判定口径(本文件逐条钉住):
- 同名目已登记过**同一个数** → 不报(与前文一致,哪怕计划里没写这个数);
- 同名目登记过**别的数** → 报;
- 台账里没有这个名目 → 退回旧的计划口径(数额不在计划里才报)。

名目匹配是保守的启发式:归一后要求共享 ≥2 个**连续汉字**;这里只喂名目差异明显的例子,
不放"共享两个字但其实是两笔钱"这种模糊样本(那种边界见模块注释里记的残余风险)。
"""
from __future__ import annotations

from app.services.numeric_ledger import (
    MIN_ITEM_RUN,
    ledger_conflicts,
    ledger_entries_from_rows,
    normalize_item,
    parse_amounts,
    parse_ledger_entries,
    same_item,
    shared_item_run_len,
)

# 台账样例:ch10 正文写出过"垫了总数=3000"(实测那条误报的来源)。
LEDGER = [{"item": "垫了总数", "amount": 3000, "chapter_idx": 10}]
# 计划钉子(与 ch10 无关的其它数),用来触发核对并验证计划口径仍在。
PLANNED = {800, 20}


# ---------------------------------------------------------------------------
# 名目归一
# ---------------------------------------------------------------------------

def test_normalize_item_keeps_hanzi_numerals():
    """汉字数词保留在名目里(常是数量或期次:"三十套校服")。"""
    assert normalize_item("合计八百四十") == "八百四十"
    assert normalize_item("  货款 3000 元。 ") == "货款元"
    assert normalize_item("(铺租),800") == "铺租"
    assert normalize_item("") == ""
    assert normalize_item(None) == ""


def test_normalize_item_drops_classification_words():
    """分类词("总额/单价")在名目里是模板词:`looks_like_total` 已经判过。"""
    assert normalize_item("校服工钱总额") == "校服工钱"
    assert normalize_item("每套单价") == ""


    assert normalize_item("合计八百四十") == "八百四十"

def test_same_item_needs_two_consecutive_hanzi():
    assert MIN_ITEM_RUN == 2
    # 同一笔钱的两种写法("三十套校服"的总工钱)= 同名目。
    assert same_item("校服工钱总额", "三十套校服总工钱") is True
    # 只差一个汉字 → 不算同名目。
    assert same_item("租", "铺租") is False
    assert same_item("铺租", "工钱") is False
    # 共享的只有分类词时不算同名目:归一后分别是"铺租"/"工钱"。
    assert same_item("铺租总额", "工钱总额") is False
    # 纯拉丁名目没有"连续汉字"证据:只有完全一致才算同名目。
    assert same_item("Rent", "Rent") is True
    assert same_item("Rent", "rent") is False
    assert same_item("", "") is False


def test_shared_item_run_len_reports_the_common_run():
    assert shared_item_run_len("校服工钱", "三十套校服总工钱") == 2
    assert shared_item_run_len("铺租", "工钱") == 0
    assert shared_item_run_len("", "铺租") == 0


# ---------------------------------------------------------------------------
# 台账解析(坏数据一律当没登记,绝不抛)
# ---------------------------------------------------------------------------


def test_parse_ledger_entries_is_defensive_about_broken_payloads():
    entries = parse_ledger_entries([
        {"item": "垫了总数", "amount": 3000, "chapter_idx": 10},
        {"item": "垫了总数", "amount": "3500.0"},
        {"item": "", "amount": 1},
        {"item": "坏数额", "amount": "三千"},
        {"item": "坏类型", "amount": {"元": 1}},
        {"amount": 1},
        "junk",
        None,
    ])
    assert [entry["item"] for entry in entries] == ["垫了总数", "垫了总数"]
    assert entries[0] == {"item": "垫了总数", "amount": 3000.0, "chapter_idx": 10}
    assert entries[1] == {"item": "垫了总数", "amount": 3500.0}


def test_parse_ledger_entries_rejects_non_list_payloads():
    assert parse_ledger_entries(None) == []
    assert parse_ledger_entries({"item": "垫了总数"}) == []
    assert parse_ledger_entries("垫了总数=3000") == []


def test_ledger_entries_from_rows_keeps_totals_only():
    rows = parse_amounts(
        '[{"item":"每套校服工钱","value":"二十八块","quote":"每套二十八块"},'
        ' {"item":"垫了总数","value":"三千","quote":"先垫了三千"},'
        ' {"item":"没数额的名目","value":"","quote":"q"}]')
    assert ledger_entries_from_rows(rows) == [{"item": "垫了总数", "amount": 3000}]


# ---------------------------------------------------------------------------
# 判定口径
# ---------------------------------------------------------------------------


def test_same_item_different_amount_is_a_conflict():
    rows = parse_amounts(
        '[{"item":"垫了总数","value":"三千五","quote":"这一回垫了三千五"}]')
    found = ledger_conflicts(rows, LEDGER, PLANNED)
    assert [row["written"] for row in found] == [3500]
    assert found[0]["item"] == "垫了总数"
    assert found[0]["quote"] == "这一回垫了三千五"
    # 台账证据随提示一起给出:作者能看到"跟哪个数对不上"。
    assert found[0]["ledger_amounts"] == [3000.0]
    assert found[0]["ledger_chapters"] == [10]


def test_same_item_same_amount_is_not_a_conflict_even_when_unplanned():
    """这条就是 ch18 的误报:3000 不在计划里(大纲与摘要都没记),但台账登记过。"""
    rows = parse_amounts(
        '[{"item":"垫了总数","value":"三千","quote":"先垫了三千"}]')
    assert 3000 not in PLANNED
    assert ledger_conflicts(rows, LEDGER, PLANNED) == []


def test_new_item_is_not_a_conflict():
    """首次出现的名目无从比对,不报(旧口径会在这里报一条)。"""
    rows = parse_amounts(
        '[{"item":"新铺面货款","value":"四百","quote":"四百块新铺面货款"}]')
    assert ledger_conflicts(rows, LEDGER, PLANNED) == []


def test_item_missing_from_ledger_falls_back_to_the_plan_rule():
    """台账里没有的名目仍走旧口径,所以"照大纲写 820"报出 840 的真阳性没有丢。"""
    rows = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百四十","quote":"一共是八百四十块整"}]')
    assert [row["written"] for row in ledger_conflicts(rows, [], {820, 800, 20})] == [840]
    # 与计划一致 → 不报(两类误报里 ch08 的那一类)。
    rows_ok = parse_amounts(
        '[{"item":"校服工钱总额","value":"八百二十","quote":"总共是八百二十块钱"}]')
    assert ledger_conflicts(rows_ok, [], {820, 800, 20}) == []


def test_planned_none_means_ledger_only_judgement():
    rows = parse_amounts(
        '[{"item":"垫了总数","value":"三千五","quote":"q"}]')
    assert [row["written"] for row in ledger_conflicts(rows, LEDGER, None)] == [3500]
    # 台账口径优先于计划口径:哪怕这个数就在计划里,与已登记的数不同一样要报。
    assert [row["written"] for row in ledger_conflicts(rows, LEDGER, {3500})] == [3500]
    # 没有台账也不做计划口径 → 一律不报(不会凭空报警)。
    assert ledger_conflicts(rows, [], None) == []


def test_unit_prices_are_never_registered_or_compared():
    rows = parse_amounts('[{"item":"每套校服工钱","value":"三十块","quote":"每套三十块"}]')
    assert ledger_entries_from_rows(rows) == []
    assert ledger_conflicts(rows, LEDGER, None) == []


def test_conflicts_are_capped():
    entries = ",".join(
        '{"item":"垫了总数%d","value":"%d元","quote":"q%d"}' % (i, 3000 + i, i)
        for i in range(10)
    )
    rows = parse_amounts(f"[{entries}]")
    assert len(ledger_conflicts(rows, LEDGER, None)) == 5


def test_broken_ledger_payload_falls_back_to_the_plan_rule_without_raising():
    rows = parse_amounts('[{"item":"垫了总数","value":"三千五","quote":"q"}]')
    # 台账读坏 → 当"没有登记过":仍按计划口径判,不抛、不凭空多报。
    assert [row["written"] for row in ledger_conflicts(rows, "not-a-ledger", PLANNED)] == [3500]
    assert ledger_conflicts(rows, None, {3500}) == []
    assert ledger_conflicts([], LEDGER, PLANNED) == []
    assert ledger_conflicts(None, LEDGER, PLANNED) == []
